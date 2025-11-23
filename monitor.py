import os
import json
import re
import requests
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Any

# ===== CONFIG & CONSTANTS =====
TARGET_URL = "https://www.anp.org.ma/_vti_bin/WS/Service.svc/mvmnv/all"
STATE_FILE = "state.json"

EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
EMAIL_TO = os.getenv("EMAIL_TO")

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587

# Statuses to monitor and remove
TARGET_STATUS = "PREVU"
# Statuses that signal the end of the port call (or removal from PREVU interest)
STATUS_TO_REMOVE = {"EN RADE", "A QUAI", "DEPART"} 
# Ports to track
ALLOWED_PORTS = {"17", "18"}

# ===== STATE =====
def load_state() -> Dict[str, str]:
    """Loads the vessel status dictionary: {vessel_id: status}"""
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            # The state now holds a dictionary of {IMO_PORTCODE: STATUS}
            return json.load(f)
    except json.JSONDecodeError:
        print(f"Warning: Could not decode {STATE_FILE}. Starting with empty state.")
        return {}


def save_state(state: Dict[str, str]):
    """Saves the current vessel status dictionary."""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# ===== HELPERS (Date formatting and Port names remain the same) =====
# ... (json_date_to_ms, json_date_to_dt, fmt_dt, fmt_time, port_name functions) ...
def json_date_to_ms(json_date: str) -> int:
    """For sorting: '/Date(1764457200000+0100)/' -> ms int."""
    if not isinstance(json_date, str):
        return 0
    m = re.search(r"/Date\((\d+)", json_date)
    if not m:
        return 0
    return int(m.group(1))


def json_date_to_dt(json_date: str):
    """For display: '/Date(1764457200000+0100)/' -> datetime with offset."""
    if not isinstance(json_date, str):
        return None
    m = re.search(r"/Date\((\d+)([+-]\d{4})?\)/", json_date)
    if not m:
        return None

    millis = int(m.group(1))
    dt = datetime.utcfromtimestamp(millis / 1000.0)

    offset_str = m.group(2)
    if offset_str:
        sign = 1 if offset_str[0] == "+" else -1
        hours = int(offset_str[1:3])
        minutes = int(offset_str[3:5])
        offset_delta = timedelta(hours=hours, minutes=minutes)
        dt += sign * offset_delta 

    return dt


def fmt_dt(json_date: str) -> str:
    """Formats the JSON date to a display string, showing only the date."""
    dt = json_date_to_dt(json_date)
    if not dt:
        return "N/A"
    return dt.strftime("%A, %d %B %Y")


def fmt_time(json_date: str) -> str:
    dt = json_date_to_dt(json_date)
    if not dt:
        return "N/A"
    return dt.strftime("%H:%M")


def port_name(code: str) -> str:
    return {"17": "Laâyoune", "18": "Dakhla"}.get(code, f"Port {code}")


def get_vessel_id(entry: dict) -> str:
    """Creates a unique ID from IMO number and Port Code."""
    imo = str(entry.get("nUMERO_LLOYDField", "NO_IMO"))
    port_code = str(entry.get("cODE_SOCIETEField", "NO_PORT"))
    return f"{imo}-{port_code}"


# ===== DATA FETCH AND PROCESSING =====

def fetch_and_process_data(current_state: Dict[str, str]) -> Tuple[Dict[str, str], Dict[str, List[Dict[str, Any]]]]:
    """
    1. Fetches data and filters for relevant ports (17/18).
    2. Compares the current live data against the stored state.
    3. Calculates the next state and identifies new vessels for notification.
    """
    print("Fetching ANP data...")
    resp = requests.get(TARGET_URL, timeout=20)
    resp.raise_for_status()
    all_data = resp.json()

    # Store all vessels currently in the feed for tracking
    live_vessels: Dict[str, Dict] = {}
    
    # Stores new vessels detected, grouped by port name
    new_vessels_by_port: Dict[str, List[Dict[str, Any]]] = {}
    
    # This will be the state to save after this run
    next_state = {}

    for entry in all_data:
        port_code = str(entry.get("cODE_SOCIETEField", ""))
        current_status = entry.get("sITUATIONField", "").upper()
        
        if port_code not in ALLOWED_PORTS:
            continue

        vessel_id = get_vessel_id(entry)
        
        # 1. Update the live vessels dict for later comparison
        live_vessels[vessel_id] = entry
        
        # 2. Determine if this entry should trigger a notification or be tracked
        
        # A. Status is 'PREVU' - this is the status we care about
        if current_status == TARGET_STATUS:
            
            # Add to the next state for tracking
            next_state[vessel_id] = current_status
            
            # Check if it's a new vessel (not in the old state)
            if vessel_id not in current_state:
                port_name_str = port_name(port_code)
                print(f"🔔 NEW PREVU vessel detected: {entry.get('nOM_NAVIREField')} at {port_name_str}")
                
                # Group for notification email
                if port_name_str not in new_vessels_by_port:
                    new_vessels_by_port[port_name_str] = []
                new_vessels_by_port[port_name_str].append(entry)

        # B. Status is not 'PREVU' (e.g., 'A QUAI', 'EN RADE')
        else:
            # We don't track non-PREVU statuses in the state
            pass 

    # 3. Clean up the state (Remove vessels that left 'PREVU' or hit 'EN RADE')
    # We iterate over the *old* state to see which tracked vessels are no longer relevant
    
    # We remove a vessel from the state if:
    # a) It's no longer in the live data (implying departure/completion)
    # b) It's in the live data, but its status changed to a 'STATUS_TO_REMOVE' status
    
    final_next_state = {}
    for v_id, status in current_state.items():
        if v_id in live_vessels:
            live_entry = live_vessels[v_id]
            live_status = live_entry.get("sITUATIONField", "").upper()
            
            if live_status == TARGET_STATUS:
                # Still PREVU, keep tracking
                final_next_state[v_id] = live_status
                
            elif live_status in STATUS_TO_REMOVE:
                # Status changed to one we want to ignore (e.g., 'EN RADE'). Remove from tracking.
                print(f"✅ Vessel {live_entry.get('nOM_NAVIREField')} changed status to {live_status}. Removing from tracking.")
                # Do not add to final_next_state
                
            else:
                # Other status change (e.g., 'PROJET'). Keep tracking if it was PREVU before.
                final_next_state[v_id] = live_status
                
        else:
            # Vessel is gone from the feed entirely, meaning port call completed. Remove from tracking.
            print(f"❌ Vessel ID {v_id} no longer in live feed. Removing from tracking.")
            # Do not add to final_next_state

    # Add back any NEW 'PREVU' vessels found in step 2.A
    for v_id, status in next_state.items():
        final_next_state[v_id] = status
        
    return final_next_state, new_vessels_by_port


# ===== EMAIL GROUPING AND SENDING =====

def format_vessel_details(entry: dict) -> str:
    """Formats a single vessel's details for the email body."""
    nom       = entry.get("nOM_NAVIREField", "")
    imo       = entry.get("nUMERO_LLOYDField", "N/A")
    cons      = entry.get("cONSIGNATAIREField", "N/A")
    eta_dt    = fmt_dt(entry.get("dATE_SITUATIONField", ""))
    prov      = entry.get("pROVField", "Inconnue")
    type_nav  = entry.get("tYP_NAVIREField", "N/A")
    num_esc   = entry.get("nUMERO_ESCALEField", "N/A")

    details = [
        f"**Nom du Navire**    : {nom}",
        f"**IMO / Lloyd's**    : {imo}",
        f"**ETA (Date Prévue)**: {eta_dt}",
        f"**Provenance**       : {prov}",
        f"**Type de Navire**   : {type_nav}",
        f"**Consignataire**    : {cons}",
        f"**Numéro d'Escale**  : {num_esc}",
    ]
    return "<br>".join(details)


def send_emails(new_vessels_by_port: Dict[str, List[Dict[str, Any]]]):
    """Sends a separate email for each port that has new vessels."""
    if not new_vessels_by_port:
        print("No emails to send.")
        return

    print(f"Preparing to send {len(new_vessels_by_port)} notification email(s)...")

    for port, vessels in new_vessels_by_port.items():
        # --- Subject ---
        if len(vessels) == 1:
            subject = f"🔔 NOUVELLE ARRIVÉE PRÉVUE | {vessels[0].get('nOM_NAVIREField')} au Port de {port}"
        else:
            subject = f"🔔 {len(vessels)} NOUVELLES ARRIVÉES PRÉVUES au Port de {port}"

        # --- Professional Body ---
        body_parts = [
            f"Bonjour,",
            "",
            f"Nous vous informons de la détection de **{len(vessels)} nouvelle(s) arrivée(s) de navire(s)** (statut **PREVU**) enregistrée(s) par l'ANP (MVM) pour le **Port de {port}**.",
            ""
        ]
        
        # Add details for each vessel
        for i, vessel in enumerate(vessels):
            body_parts.append("---")
            body_parts.append(f"**Détails Navire #{i+1}**:")
            body_parts.append(format_vessel_details(vessel))

        body_parts.extend([
            "",
            "---",
            f"Cette notification est basée sur les données ANP pour les statuts '{TARGET_STATUS}' du Port de {port}.",
            "Cordialement,",
        ])

        # Use HTML for better formatting (bolding, line breaks)
        body_html = "<br>".join(body_parts).replace(":", "</b>:", 1).replace(":", ":") # Simple trick to bold labels

        msg = MIMEText(body_html, "html", "utf-8")
        msg["Subject"] = subject
        msg["From"] = EMAIL_USER
        msg["To"] = EMAIL_TO

        try:
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
                server.starttls()
                server.login(EMAIL_USER, EMAIL_PASS)
                server.sendmail(EMAIL_USER, [EMAIL_TO], msg.as_string())
            print(f"Email sent successfully for Port: {port}")
        except Exception as e:
            print(f"ERROR: Failed to send email for Port {port}. Details: {e}")


# ===== MAIN EXECUTION =====
def main():
    current_state = load_state()

    try:
        # Fetch, compare against state, and get the next state and new vessels
        next_state, new_vessels_by_port = fetch_and_process_data(current_state)
    except Exception as e:
        print(f"Critical error during data fetching or processing: {e}")
        return

    # Check if there were any changes (new vessels OR status removals)
    if current_state != next_state:
        # 1. Send notifications for new vessels
        send_emails(new_vessels_by_port)
        
        # 2. Save the updated state (reflecting new PREVU vessels and removed/changed status vessels)
        save_state(next_state)
        print("State updated successfully.")
    else:
        print("No new 'PREVU' vessels detected and no state changes required.")


if __name__ == "__main__":
    main()

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

# ⬇️ STATE PERSISTENCE SETTINGS FOR GITHUB ACTIONS ⬇️
# Environment variable to read the state from (must match GitHub Secret name)
STATE_ENV_VAR = "VESSEL_STATE_DATA" 
# Temporary file to write the new state to for the CI job to pick up
TEMP_OUTPUT_FILE = 'state_output.txt' 

# Email credentials fetched from environment variables
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

# ===== STATE MANAGEMENT (PERSISTENT) =====
def load_state() -> Dict[str, str]:
    """Loads the vessel status dictionary from the persistent environment variable."""
    state_data = os.getenv(STATE_ENV_VAR)
    print(f"DEBUG: Attempting to load state from env var '{STATE_ENV_VAR}'...")

    if not state_data:
        print("DEBUG: Environment variable is empty or not set. Starting with empty state.")
        return {}
    try:
        # State data is stored as a JSON string in the environment variable
        state = json.loads(state_data)
        print(f"DEBUG: State loaded successfully (from environment). Tracked vessels: {len(state)}")
        return state
    except json.JSONDecodeError:
        print("DEBUG: WARNING! Could not decode state from environment variable. Starting with empty state.")
        return {}


def save_state(state: Dict[str, str]):
    """Saves the current vessel status (as a JSON string) to a temporary file 
       that will be used by the GitHub Actions workflow to update the secret."""
    
    updated_json_string = json.dumps(state)
    
    print(f"DEBUG: Saving new state ({len(state)} vessels) to temporary file '{TEMP_OUTPUT_FILE}'...")
    try:
        # Write the new JSON string to a file in the temporary workspace
        with open(TEMP_OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write(updated_json_string)
        print(f"DEBUG: State successfully written to '{TEMP_OUTPUT_FILE}' for CI update.")
    except IOError as e:
        print(f"CRITICAL ERROR: Failed to write to temp file {TEMP_OUTPUT_FILE}. Check CI permissions. Details: {e}")


# ===== HELPERS (Date, Port, ID Formatting) =====
def json_date_to_ms(json_date: str) -> int:
    """Converts ANP JSON date format to milliseconds for sorting."""
    if not isinstance(json_date, str):
        return 0
    m = re.search(r"/Date\((\d+)", json_date)
    if not m:
        return 0
    return int(m.group(1))


def json_date_to_dt(json_date: str):
    """Converts ANP JSON date format to a Python datetime object."""
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
    """Formats date to remove time: 'Sunday, 30 November 2025'"""
    dt = json_date_to_dt(json_date)
    if not dt:
        return "N/A"
    return dt.strftime("%A, %d %B %Y")


def port_name(code: str) -> str:
    """Maps port codes to names."""
    return {"17": "Laâyoune", "18": "Dakhla"}.get(code, f"Port {code}")


def get_vessel_id(entry: dict) -> str:
    """Creates a unique ID from IMO number and Port Code."""
    imo = str(entry.get("nUMERO_LLOYDField", "NO_IMO"))
    port_code = str(entry.get("cODE_SOCIETEField", "NO_PORT"))
    return f"{imo}-{port_code}"


# ===== DATA FETCH AND PROCESSING =====

def fetch_and_process_data(current_state: Dict[str, str]) -> Tuple[Dict[str, str], Dict[str, List[Dict[str, Any]]]]:
    """Fetches data, compares it against the stored state, and identifies new vessels."""
    print("Fetching ANP data...")
    try:
        resp = requests.get(TARGET_URL, timeout=20)
        resp.raise_for_status()
        all_data = resp.json()
    except requests.exceptions.RequestException as e:
        print(f"CRITICAL ERROR: Failed to fetch data from ANP. Details: {e}")
        # On fetch failure, return the current state to prevent data loss
        return current_state, {}

    live_vessels: Dict[str, Dict] = {}
    new_vessels_by_port: Dict[str, List[Dict[str, Any]]] = {}
    next_state = {} 

    # 1. IDENTIFY NEW PREVU VESSELS AND BUILD TEMPORARY NEXT STATE
    for entry in all_data:
        port_code = str(entry.get("cODE_SOCIETEField", ""))
        current_status = entry.get("sITUATIONField", "").upper()
        
        if port_code not in ALLOWED_PORTS:
            continue

        vessel_id = get_vessel_id(entry)
        live_vessels[vessel_id] = entry
        
        if current_status == TARGET_STATUS:
            next_state[vessel_id] = current_status
            
            # Found a PREVU vessel that was NOT in the old state -> New arrival!
            if vessel_id not in current_state:
                port_name_str = port_name(port_code)
                print(f"🔔 NEW PREVU vessel detected: {entry.get('nOM_NAVIREField')} ({vessel_id}) at {port_name_str}")
                
                if port_name_str not in new_vessels_by_port:
                    new_vessels_by_port[port_name_str] = []
                new_vessels_by_port[port_name_str].append(entry)

    # 2. DETERMINE FINAL NEXT STATE (CLEANUP)
    final_next_state = {}
    vessels_removed_count = 0
    
    for v_id, status in current_state.items():
        if v_id in live_vessels:
            live_entry = live_vessels[v_id]
            live_status = live_entry.get("sITUATIONField", "").upper()
            
            if live_status == TARGET_STATUS:
                final_next_state[v_id] = live_status
            elif live_status in STATUS_TO_REMOVE:
                print(f"DEBUG: ✅ Vessel {live_entry.get('nOM_NAVIREField')} ({v_id}) changed status to {live_status}. REMOVING from tracking.")
                vessels_removed_count += 1
            else:
                # Keep tracking other statuses for now, if it was already tracked
                final_next_state[v_id] = live_status
        else:
            # Vessel is gone from the feed entirely (completed call). Remove from tracking.
            print(f"DEBUG: ❌ Vessel ID {v_id} no longer in live feed. REMOVING from tracking.")
            vessels_removed_count += 1

    # Add new PREVU vessels found in step 1 to the final state
    for v_id, status in next_state.items():
        final_next_state[v_id] = status
        
    print(f"DEBUG: Vessels removed from tracking this run: {vessels_removed_count}")
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
        print("DEBUG: No emails to send.")
        return

    print(f"Preparing to send {len(new_vessels_by_port)} notification email(s)...")

    for port, vessels in new_vessels_by_port.items():
        if len(vessels) == 1:
            subject = f"🔔 NOUVELLE ARRIVÉE PRÉVUE | {vessels[0].get('nOM_NAVIREField')} au Port de {port}"
        else:
            subject = f"🔔 {len(vessels)} NOUVELLES ARRIVÉES PRÉVUES au Port de {port}"

        body_parts = [
            f"Bonjour,",
            "",
            f"Nous vous informons de la détection de **{len(vessels)} nouvelle(s) arrivée(s) de navire(s)** (statut **PREVU**) enregistrée(s) par l'ANP (MVM) pour le **Port de {port}**.",
            ""
        ]
        
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

        # Simple HTML formatting: replace the first colon with bold closing tag
        body_html = "<br>".join(body_parts).replace(":", "</b>:", 1).replace(":", ":")

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
            print(f"ERROR: Failed to send email for Port {port}. Check credentials/network. Details: {e}")


# ===== MAIN EXECUTION =====
def main():
    print("-" * 50)
    print(f"Monitoring run started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    current_state = load_state()

    try:
        next_state, new_vessels_by_port = fetch_and_process_data(current_state)
    except Exception as e:
        print(f"Critical error during processing: {e}")
        return

    # Check for any state change (new vessels OR status removals)
    if current_state != next_state:
        print(f"DEBUG: State change detected! Old count: {len(current_state)}, New count: {len(next_state)}")
        
        # 1. Send notifications for new vessels
        if new_vessels_by_port:
            send_emails(new_vessels_by_port)
        else:
            print("DEBUG: State change was due only to vessel removal/status change, no new emails sent.")
        
        # 2. Save the updated state to the temporary output file
        save_state(next_state)
    else:
        print("No new 'PREVU' vessels detected and no state changes required.")
    
    print("-" * 50)


if __name__ == "__main__":
    main()

import os
import json
import re
import requests
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta

# ===== CONFIG =====
TARGET_URL = "https://www.anp.org.ma/_vti_bin/WS/Service.svc/mvmnv/all"
STATE_FILE = "state.json"

EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
EMAIL_TO = os.getenv("EMAIL_TO")

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587

# Define the status to monitor for new notifications
TARGET_STATUS = "PREVU"

# ===== STATE =====
def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        print(f"Warning: Could not decode {STATE_FILE}. Starting with empty state.")
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# ===== HELPERS =====
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
    # Python's utcfromtimestamp doesn't handle offsets, so we parse it manually
    dt = datetime.utcfromtimestamp(millis / 1000.0)

    offset_str = m.group(2)
    if offset_str:
        # Determine the offset time delta
        sign = 1 if offset_str[0] == "+" else -1
        hours = int(offset_str[1:3])
        minutes = int(offset_str[3:5])
        offset_delta = timedelta(hours=hours, minutes=minutes)
        
        # Apply the offset to get the local time reported by the API
        dt += sign * offset_delta 

    return dt


def fmt_dt(json_date: str) -> str:
    dt = json_date_to_dt(json_date)
    if not dt:
        return "N/A"
    return dt.strftime("%A, %d %B %Y à %H:%M") # Formatted for Pro Email


def fmt_time(json_date: str) -> str:
    dt = json_date_to_dt(json_date)
    if not dt:
        return "N/A"
    return dt.strftime("%H:%M")


def port_name(code: str) -> str:
    # Adding a default case for clarity
    return {"17": "Laâyoune", "18": "Dakhla"}.get(code, f"Port {code}")


# ===== FETCH LATEST ENTRY (17 & 18) - ONLY PREVU STATUS =====
def fetch_latest_row_fingerprint():
    """Fetches the newest entry with status 'PREVU' for ports 17 or 18."""
    print(f"Fetching latest row from ANP JSON, looking for status: {TARGET_STATUS}...")

    resp = requests.get(TARGET_URL, timeout=20)
    resp.raise_for_status()

    data = resp.json()
    if not isinstance(data, list):
        raise RuntimeError("API did not return a JSON list.")

    # Only Laâyoune (17) and Dakhla (18)
    allowed_ports = {"17", "18"}
    
    # 1. Filter by Port and Target Status ("PREVU")
    filtered_and_status = [
        v for v in data
        if str(v.get("cODE_SOCIETEField")) in allowed_ports
        and v.get("sITUATIONField", "").upper() == TARGET_STATUS
    ]

    if not filtered_and_status:
        print(f"No entries found with status '{TARGET_STATUS}' for ports 17 or 18.")
        # Return None, None if no relevant entry is found
        return None, None 

    # 2. Sort newest by dATE_SITUATIONField
    filtered_and_status.sort(
        key=lambda v: json_date_to_ms(v.get("dATE_SITUATIONField", "")),
        reverse=True,
    )

    latest = filtered_and_status[0]

    # Use a limited set of keys for the fingerprint to track the essential ID of the PREVU entry
    # This ensures that if only non-essential fields change, we don't spam.
    # We use Ship name, IMO, Port, and ETA/Situation Date for a solid unique ID.
    essential_keys = [
        "nOM_NAVIREField", "nUMERO_LLOYDField", "cODE_SOCIETEField", 
        "dATE_SITUATIONField", "sITUATIONField"
    ]
    
    fingerprint_data = {k: latest.get(k) for k in essential_keys}
    fingerprint = json.dumps(fingerprint_data, sort_keys=True, ensure_ascii=False)
    
    return fingerprint, latest


# ===== EMAIL (Professional Template) =====
def send_email(entry: dict):
    # Map JSON -> your columns
    port_code = str(entry.get("cODE_SOCIETEField", ""))
    port      = port_name(port_code)
    nom       = entry.get("nOM_NAVIREField", "")
    imo       = entry.get("nUMERO_LLOYDField", "N/A")
    cons      = entry.get("cONSIGNATAIREField", "N/A")
    eta_dt    = fmt_dt(entry.get("dATE_SITUATIONField", ""))
    prov      = entry.get("pROVField", "Inconnue")
    type_nav  = entry.get("tYP_NAVIREField", "N/A")
    num_esc   = entry.get("nUMERO_ESCALEField", "N/A")

    # --- Subject ---
    subject = f"🔔 NOUVELLE ARRIVÉE PRÉVUE | {nom} ({imo}) au Port de {port}"

    # --- Professional Body ---
    body_lines = [
        f"Bonjour,",
        "",
        f"Nous vous informons de la détection d'une **nouvelle arrivée de navire** enregistrée par l'ANP (MVM) pour le port de **{port}**.",
        "",
        "**Détails de l'arrivée (Statut PREVU):**",
        "--------------------------------------",
        f"**Nom du Navire**    : {nom}",
        f"**IMO / Lloyd's**    : {imo}",
        f"**Port de Destination**: {port}",
        f"**Situation (ANP)**  : {TARGET_STATUS} (Prévue)",
        "",
        f"**ETA (Date Situation):** {eta_dt}",
        f"**Provenance**       : {prov}",
        f"**Type de Navire**   : {type_nav}",
        f"**Consignataire**    : {cons}",
        f"**Numéro d'Escale**  : {num_esc}",
        "--------------------------------------",
        "",
        "Cette notification est basée sur la dernière mise à jour disponible pour les statuts 'PREVU' des ports 17 et 18.",
        "Cordialement,",
    ]

    # Use HTML for better formatting (bolding, line breaks)
    body_html = "<br>".join(body_lines).replace(":", "</b>:") # Simple trick to bold labels

    msg = MIMEText(body_html, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_TO

    print("Sending email...")

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASS)
            server.sendmail(EMAIL_USER, [EMAIL_TO], msg.as_string())
        print("Email sent successfully.")
    except Exception as e:
        print(f"ERROR: Failed to send email. Check credentials/network. Details: {e}")


# ===== MAIN =====
def main():
    state = load_state()
    last_fp = state.get("last_fingerprint_prevu")

    current_fp, latest_entry = fetch_latest_row_fingerprint()

    if latest_entry is None:
        print(f"No vessel with status '{TARGET_STATUS}' to track currently.")
        # Do not change the state if no PREVU vessel is found
        return

    if current_fp != last_fp:
        print(f"🔔 NEW PREVU ENTRY DETECTED or UPDATED for {latest_entry.get('nOM_NAVIREField')}.")
        send_email(latest_entry)
        
        # Save the new PREVU entry's fingerprint
        state["last_fingerprint_prevu"] = current_fp
        save_state(state)
    else:
        print(f"Latest '{TARGET_STATUS}' vessel has not changed or is still being tracked.")


if __name__ == "__main__":
    main()

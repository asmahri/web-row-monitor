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

# State persistence (GitHub Actions)
STATE_ENV_VAR = "VESSEL_STATE_DATA"      # GitHub Secret name
TEMP_OUTPUT_FILE = "state_output.txt"    # temp file used by workflow to update secret

# Email (from GitHub Secrets)
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
EMAIL_TO = os.getenv("EMAIL_TO")
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
EMAIL_ENABLED = os.getenv("EMAIL_ENABLED", "true").lower() == "true"

# CallMeBot (WhatsApp) – all from GitHub Secrets
CALLMEBOT_PHONE = os.getenv("CALLMEBOT_PHONE")
CALLMEBOT_APIKEY = os.getenv("CALLMEBOT_APIKEY")
CALLMEBOT_ENABLED = os.getenv("CALLMEBOT_ENABLED", "true").lower() == "true"
CALLMEBOT_API_URL = "https://api.callmebot.com/whatsapp.php"

# Status / ports
TARGET_STATUS = "PREVU"
STATUS_TO_REMOVE = {"EN RADE", "A QUAI", "DEPART"}
# 16: Tan Tan, 17: Laâyoune, 18: Dakhla
ALLOWED_PORTS = {"16", "17", "18"}


# ===== STATE MANAGEMENT =====
def load_state() -> Dict[str, str]:
    """Loads the vessel status dictionary from the persistent environment variable."""
    state_data = os.getenv(STATE_ENV_VAR)
    print(f"DEBUG: Attempting to load state from env var '{STATE_ENV_VAR}'...")

    if not state_data:
        print("DEBUG: Environment variable is empty or not set. Starting with empty state.")
        return {}
    try:
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
        with open(TEMP_OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write(updated_json_string)
        print(f"DEBUG: State successfully written to '{TEMP_OUTPUT_FILE}' for CI update.")
    except IOError as e:
        print(f"CRITICAL ERROR: Failed to write to temp file {TEMP_OUTPUT_FILE}. Details: {e}")


# ===== HELPERS (DATE / PORT / IDs) =====
def json_date_to_ms(json_date: str) -> int:
    """Converts ANP JSON date format to milliseconds for sorting."""
    if not isinstance(json_date, str):
        return 0
    m = re.search(r"/Date\((\d+)", json_date)
    if not m:
        return 0
    return int(m.group(1))


def json_date_to_dt(json_date: str):
    """
    Convertit la date ANP en datetime locale en tenant compte de l'offset (+0100).
    """
    if not isinstance(json_date, str):
        return None
    m = re.search(r"/Date\((\d+)([+-]\d{4})?\)/", json_date)
    if not m:
        return None

    millis = int(m.group(1))
    dt = datetime.utcfromtimestamp(millis / 1000.0)  # base UTC

    offset_str = m.group(2)
    if offset_str:
        sign = 1 if offset_str[0] == "+" else -1
        hours = int(offset_str[1:3])
        minutes = int(offset_str[3:5])
        offset_delta = timedelta(hours=hours, minutes=minutes)
        dt += sign * offset_delta  # applique l'offset une seule fois

    return dt


def fmt_dt(json_date: str) -> str:
    """Formate DATE_SITUATION en français : 'Dimanche, 30 novembre 2025'."""
    dt = json_date_to_dt(json_date)
    if not dt:
        return "N/A"

    jours = [
        "lundi", "mardi", "mercredi", "jeudi",
        "vendredi", "samedi", "dimanche"
    ]
    mois = [
        "janvier", "février", "mars", "avril", "mai", "juin",
        "juillet", "août", "septembre", "octobre", "novembre", "décembre"
    ]

    jour_nom = jours[dt.weekday()].capitalize()
    mois_nom = mois[dt.month - 1]

    return f"{jour_nom}, {dt.day:02d} {mois_nom} {dt.year}"


def fmt_time_only(json_date: str) -> str:
    """Formats HEURE_SITUATION as time only: '14:00'."""
    dt = json_date_to_dt(json_date)
    if not dt:
        return "N/A"
    return dt.strftime("%H:%M")


def port_name(code: str) -> str:
    """Maps port codes to names."""
    return {
        "16": "Tan Tan",
        "17": "Laâyoune",
        "18": "Dakhla",
    }.get(code, f"Port {code}")


def get_vessel_id(entry: dict) -> str:
    """Creates a unique ID from IMO number and Port Code."""
    imo = str(entry.get("nUMERO_LLOYDField", "NO_IMO"))
    port_code = str(entry.get("cODE_SOCIETEField", "NO_PORT"))
    return f"{imo}-{port_code}"


# ===== FETCH & PROCESS DATA =====
def fetch_and_process_data(
    current_state: Dict[str, str]
) -> Tuple[Dict[str, str], Dict[str, List[Dict[str, Any]]]]:
    """Fetches data, compares it against the stored state, and identifies new vessels."""
    print("Fetching ANP data...")
    try:
        resp = requests.get(TARGET_URL, timeout=20)
        resp.raise_for_status()
        all_data = resp.json()
    except requests.exceptions.RequestException as e:
        print(f"CRITICAL ERROR: Failed to fetch data from ANP. Details: {e}")
        return current_state, {}

    live_vessels: Dict[str, Dict] = {}
    new_vessels_by_port: Dict[str, List[Dict[str, Any]]] = {}
    next_state: Dict[str, str] = {}

    # 1. Identify PREVU vessels & build next_state
    for entry in all_data:
        port_code = str(entry.get("cODE_SOCIETEField", ""))
        current_status = entry.get("sITUATIONField", "").upper()

        if port_code not in ALLOWED_PORTS:
            continue

        vessel_id = get_vessel_id(entry)
        live_vessels[vessel_id] = entry

        if current_status == TARGET_STATUS:
            next_state[vessel_id] = current_status

            # New PREVU vessel (not tracked before)
            if vessel_id not in current_state:
                port_name_str = port_name(port_code)
                print(f"🔔 NEW PREVU vessel detected: {entry.get('nOM_NAVIREField')} ({vessel_id}) at {port_name_str}")
                if port_name_str not in new_vessels_by_port:
                    new_vessels_by_port[port_name_str] = []
                new_vessels_by_port[port_name_str].append(entry)

    # 2. Cleanup tracking (removals / status change)
    final_next_state: Dict[str, str] = {}
    vessels_removed_count = 0

    for v_id, status in current_state.items():
        if v_id in live_vessels:
            live_entry = live_vessels[v_id]
            live_status = live_entry.get("sITUATIONField", "").upper()

            if live_status == TARGET_STATUS:
                final_next_state[v_id] = live_status
            elif live_status in STATUS_TO_REMOVE:
                print(
                    f"DEBUG: ✅ Vessel {live_entry.get('nOM_NAVIREField')} ({v_id}) "
                    f"changed status to {live_status}. REMOVING from tracking."
                )
                vessels_removed_count += 1
            else:
                final_next_state[v_id] = live_status
        else:
            print(
                f"DEBUG: ❌ Vessel ID {v_id} no longer in live feed. REMOVING from tracking."
            )
            vessels_removed_count += 1

    # Add newly found PREVU vessels
    for v_id, status in next_state.items():
        final_next_state[v_id] = status

    print(f"DEBUG: Vessels removed from tracking this run: {vessels_removed_count}")
    return final_next_state, new_vessels_by_port


# ===== FORMATTERS (EMAIL + WHATSAPP) =====
def format_vessel_details(entry: dict) -> str:
    """Formats a single vessel's details for the email body (premium card design)."""
    nom = entry.get("nOM_NAVIREField", "")
    imo = entry.get("nUMERO_LLOYDField", "N/A")
    cons = entry.get("cONSIGNATAIREField", "N/A")
    eta_date = fmt_dt(entry.get("dATE_SITUATIONField", ""))
    eta_time = fmt_time_only(entry.get("hEURE_SITUATIONField", ""))
    prov = entry.get("pROVField", "Inconnue")
    type_nav = entry.get("tYP_NAVIREField", "N/A")
    num_esc = entry.get("nUMERO_ESCALEField", "N/A")

    eta_line = f"{eta_date} {eta_time}".strip()

    return f"""
<div style="
    font-family:Arial, sans-serif;
    font-size:14px;
    margin:15px 0;
    padding:0;
">
  <div style="
      border:1px solid #d0d7e1;
      border-radius:10px;
      overflow:hidden;
      box-shadow:0 2px 6px rgba(0,0,0,0.08);
  ">
    <div style="
        background:#0a3d62;
        color:white;
        padding:12px 15px;
        font-size:16px;
    ">
      🚢 <b>{nom}</b>
      <span style="
          background:#1dd1a1;
          color:#003f2e;
          padding:3px 8px;
          border-radius:6px;
          font-size:12px;
          float:right;
      ">
        {TARGET_STATUS}
      </span>
    </div>

    <table style="width:100%; border-collapse:collapse;">
      <tr style="background:#f8faff;">
        <td style="padding:10px; border-bottom:1px solid #e6e9ef; width:35%;"><b>🆔 IMO</b></td>
        <td style="padding:10px; border-bottom:1px solid #e6e9ef;">{imo}</td>
      </tr>

      <tr style="background:white;">
        <td style="padding:10px; border-bottom:1px solid #e6e9ef;"><b>🕒 ETA</b></td>
        <td style="padding:10px; border-bottom:1px solid #e6e9ef;">{eta_line}</td>
      </tr>

      <tr style="background:#f8faff;">
        <td style="padding:10px; border-bottom:1px solid #e6e9ef;"><b>🌍 Provenance</b></td>
        <td style="padding:10px; border-bottom:1px solid #e6e9ef;">{prov}</td>
      </tr>

      <tr style="background:white;">
        <td style="padding:10px; border-bottom:1px solid #e6e9ef;"><b>🛳️ Type</b></td>
        <td style="padding:10px; border-bottom:1px solid #e6e9ef;">{type_nav}</td>
      </tr>

      <tr style="background:#f8faff;">
        <td style="padding:10px; border-bottom:1px solid #e6e9ef;"><b>🏢 Consignataire</b></td>
        <td style="padding:10px; border-bottom:1px solid #e6e9ef;">{cons}</td>
      </tr>

      <tr style="background:white;">
        <td style="padding:10px;"><b>📝 Escale</b></td>
        <td style="padding:10px;">{num_esc}</td>
      </tr>
    </table>
  </div>
</div>
""".strip()


def format_vessel_for_whatsapp(entry: dict) -> str:
    """Formats a single vessel's details for WhatsApp (style emojis)."""
    nom = entry.get("nOM_NAVIREField", "")
    imo = entry.get("nUMERO_LLOYDField", "N/A")
    eta_date = fmt_dt(entry.get("dATE_SITUATIONField", ""))
    eta_time = fmt_time_only(entry.get("hEURE_SITUATIONField", ""))
    prov = entry.get("pROVField", "Inconnue")
    cons = entry.get("cONSIGNATAIREField", "N/A")
    num_esc = entry.get("nUMERO_ESCALEField", "N/A")

    eta_line = f"{eta_date} {eta_time}".strip()

    return (
        f"🚢 *{nom}*\n"
        f"🔹 IMO : {imo}\n"
        f"📅 ETA : {eta_line}\n"
        f"📍 Prov : {prov}\n"
        f"🏢 Cons : {cons}\n"
        f"📝 Escale : {num_esc}"
    )


# ===== EMAIL SENDING =====
def send_emails(new_vessels_by_port: Dict[str, List[Dict[str, Any]]]):
    """Sends a separate email for each port that has new vessels."""
    if not EMAIL_ENABLED:
        print("DEBUG: Email notifications disabled.")
        return

    if not new_vessels_by_port:
        print("DEBUG: No emails to send.")
        return

    print(f"Preparing to send {len(new_vessels_by_port)} notification email(s)...")

    for port, vessels in new_vessels_by_port.items():
        if len(vessels) == 1:
            subject = (
                f"🔔 NOUVELLE ARRIVÉE PRÉVUE | "
                f"{vessels[0].get('nOM_NAVIREField')} au Port de {port}"
            )
        else:
            subject = f"🔔 {len(vessels)} NOUVELLES ARRIVÉES PRÉVUES au Port de {port}"

        body_parts: List[str] = [
            "Bonjour,",
            "",
            (
                f"Nous vous informons de la détection de <b>{len(vessels)} nouvelle(s) "
                f"arrivée(s) de navire(s)</b> (statut <b>{TARGET_STATUS}</b>) "
                f"enregistrée(s) par l'ANP (MVM) pour le <b>Port de {port}</b>."
            ),
            "",
        ]

        for vessel in vessels:
            body_parts.append("<hr>")
            body_parts.append(format_vessel_details(vessel))

        body_parts.extend(
            [
                "",
                "<hr>",
                (
                    f"Cette notification est basée sur les données ANP pour les "
                    f"statuts <b>{TARGET_STATUS}</b> du Port de {port}."
                ),
                "Cordialement,",
            ]
        )

        body_html = "<br>".join(body_parts)

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
            print(
                f"ERROR: Failed to send email for Port {port}. "
                f"Check credentials/network. Details: {e}"
            )


# ===== WHATSAPP (CALLMEBOT) SENDING =====
def send_whatsapp_notifications(
    new_vessels_by_port: Dict[str, List[Dict[str, Any]]]
):
    """Send one WhatsApp message per port using CallMeBot."""
    if not CALLMEBOT_ENABLED:
        print("DEBUG: CallMeBot disabled by CALLMEBOT_ENABLED.")
        return

    if not CALLMEBOT_PHONE or not CALLMEBOT_APIKEY:
        print("DEBUG: CallMeBot phone/apikey not configured. Skipping WhatsApp.")
        return

    if not new_vessels_by_port:
        print("DEBUG: No WhatsApp notifications to send.")
        return

    for port, vessels in new_vessels_by_port.items():
        header = f"ANP MVM – {len(vessels)} nouveau(x) PREVU au port de {port}"

        parts: List[str] = [header, ""]
        for i, vessel in enumerate(vessels, start=1):
            parts.append(f"--- Navire #{i} ---")
            parts.append(format_vessel_for_whatsapp(vessel))
            parts.append("")

        text = "\n".join(parts)

        try:
            print(f"DEBUG: Sending WhatsApp notification via CallMeBot for port {port}...")
            r = requests.get(
                CALLMEBOT_API_URL,
                params={
                    "phone": CALLMEBOT_PHONE,
                    "apikey": CALLMEBOT_APIKEY,
                    "text": text,
                },
                timeout=20,
            )
            r.raise_for_status()
            print(f"WhatsApp notification sent successfully for Port: {port}")
        except Exception as e:
            print(
                f"ERROR: Failed to send WhatsApp notification for Port {port}. "
                f"Details: {e}"
            )


# ===== MAIN =====
def main():
    print("-" * 50)
    print(f"Monitoring run started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"DEBUG: ENV CALLMEBOT_PHONE set? {bool(CALLMEBOT_PHONE)}")
    print(f"DEBUG: ENV CALLMEBOT_APIKEY set? {bool(CALLMEBOT_APIKEY)}")
    print(f"DEBUG: EMAIL_ENABLED = {EMAIL_ENABLED}")

    current_state = load_state()

    try:
        next_state, new_vessels_by_port = fetch_and_process_data(current_state)
    except Exception as e:
        print(f"Critical error during processing: {e}")
        return

    if current_state != next_state:
        print(
            f"DEBUG: State change detected! "
            f"Old count: {len(current_state)}, New count: {len(next_state)}"
        )

        if new_vessels_by_port:
            send_emails(new_vessels_by_port)
            send_whatsapp_notifications(new_vessels_by_port)
        else:
            print(
                "DEBUG: State change was due only to vessel removal/status change, "
                "no new notifications sent."
            )

        save_state(next_state)
    else:
        print("No new 'PREVU' vessels detected and no state changes required.")

    print("-" * 50)


if __name__ == "__main__":
    main()

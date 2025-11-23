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
EMAIL_TO   = os.getenv("EMAIL_TO")

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587


# ===== STATE =====
def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)


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
    dt = datetime.utcfromtimestamp(millis / 1000.0)

    offset_str = m.group(2)
    if offset_str:
        sign = 1 if offset_str[0] == "+" else -1
        hours = int(offset_str[1:3])
        minutes = int(offset_str[3:5])
        dt += sign * timedelta(hours=hours, minutes=minutes)

    return dt


def fmt_dt(json_date: str) -> str:
    dt = json_date_to_dt(json_date)
    if not dt:
        return ""
    return dt.strftime("%d/%m/%Y %H:%M")


def fmt_time(json_date: str) -> str:
    dt = json_date_to_dt(json_date)
    if not dt:
        return ""
    return dt.strftime("%H:%M")


def port_name(code: str) -> str:
    return {"17": "Laâyoune", "18": "Dakhla"}.get(code, code)


# ===== FETCH LATEST ENTRY (17 & 18) =====
def fetch_latest_row_fingerprint():
    print("Fetching latest row from ANP JSON...")

    resp = requests.get(TARGET_URL, timeout=20)
    resp.raise_for_status()

    data = resp.json()
    if not isinstance(data, list):
        raise RuntimeError("API did not return a JSON list.")

    # Only Laâyoune (17) and Dakhla (18)
    allowed_ports = {"17", "18"}
    filtered = [
        v for v in data
        if str(v.get("cODE_SOCIETEField")) in allowed_ports
    ]

    if not filtered:
        raise RuntimeError("No entries for ports 17 or 18.")

    # Sort newest by dATE_SITUATIONField
    filtered.sort(
        key=lambda v: json_date_to_ms(v.get("dATE_SITUATIONField", "")),
        reverse=True,
    )

    latest = filtered[0]

    fingerprint = json.dumps(latest, sort_keys=True, ensure_ascii=False)
    return fingerprint, latest


# ===== EMAIL =====
def send_email(entry: dict):
    # Map JSON -> your columns
    port      = port_name(str(entry.get("cODE_SOCIETEField", "")))   # no code, name only
    nom       = entry.get("nOM_NAVIREField", "")
    imo       = entry.get("nUMERO_LLOYDField", "")
    cons      = entry.get("cONSIGNATAIREField", "")
    eta_dt    = fmt_dt(entry.get("dATE_SITUATIONField", ""))
    heure_loc = fmt_time(entry.get("hEURE_SITUATIONField", ""))
    prov      = entry.get("pROVField", "")
    type_nav  = entry.get("tYP_NAVIREField", "")
    situ      = entry.get("sITUATIONField", "")
    num_esc   = entry.get("nUMERO_ESCALEField", "")

    subject = f"ANP MVM – {situ} – {nom} ({port})"

    # Body in same logic as your Excel headers
    body_lines = [
        "Changement détecté dans ANP MVM (ports 17/18)",
        "",
        f"Port              : {port}",
        f"Nom du Navire     : {nom}",
        f"IMO               : {imo}",
        f"Consignataire     : {cons}",
        f"ETA_DateTime      : {eta_dt}",
        f"Heure_Local       : {heure_loc}",
        f"Provenance        : {prov}",
        f"Type du Navire    : {type_nav}",
        f"Situation         : {situ}",
        f"Numéro d'escale   : {num_esc}",
    ]

    body = "\n".join(body_lines)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_TO

    print("Sending email...")

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(EMAIL_USER, EMAIL_PASS)
        server.sendmail(EMAIL_USER, [EMAIL_TO], msg.as_string())

    print("Email sent.")


# ===== MAIN =====
def main():
    state = load_state()
    last_fp = state.get("last_fingerprint")

    current_fp, latest_entry = fetch_latest_row_fingerprint()

    if current_fp != last_fp:
        print("🔔 CHANGE DETECTED")
        send_email(latest_entry)
        state["last_fingerprint"] = current_fp
        save_state(state)
    else:
        print("No change.")


if __name__ == "__main__":
    main()

import os
import json
import re
import requests
import smtplib
from email.mime.text import MIMEText

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
    """
    Convert ANP style '/Date(1717106400000+0000)/' to milliseconds int.
    """
    if not isinstance(json_date, str):
        return 0
    m = re.search(r"/Date\((\d+)", json_date)
    if not m:
        return 0
    return int(m.group(1))


# ===== FETCH LATEST ENTRY (for ports 17 & 18) =====
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

    # Newest by dATE_SITUATIONField
    filtered.sort(
        key=lambda v: json_date_to_ms(v.get("dATE_SITUATIONField", "")),
        reverse=True,
    )

    latest = filtered[0]

    # Fingerprint: full JSON of latest entry
    fingerprint = json.dumps(latest, sort_keys=True, ensure_ascii=False)
    return fingerprint, latest


# ===== EMAIL =====
def port_name(code: str) -> str:
    return {"17": "Laâyoune", "18": "Dakhla"}.get(code, code)


def send_email(entry: dict):
    vessel = entry.get("nOM_NAVIREField", "Unknown")
    port_code = str(entry.get("cODE_SOCIETEField", ""))
    port = port_name(port_code)
    situation = entry.get("sITUATIONField", "")

    subject = f"ANP MVM update - {vessel} ({port})"

    body_lines = [
        "Change detected in ANP MVM (ports 17/18):",
        "",
        f"Vessel : {vessel}",
        f"Port   : {port} ({port_code})",
        f"Status : {situation}",
        "",
        "Raw JSON entry:",
        json.dumps(entry, indent=2, ensure_ascii=False),
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

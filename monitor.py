import os
import json
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


# ===== UTIL =====
def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# ===== FETCH LATEST ENTRY =====
def fetch_latest_row():
    print("Fetching ANP JSON...")

    response = requests.get(TARGET_URL, timeout=20)
    response.raise_for_status()

    data = response.json()
    if not isinstance(data, list):
        raise RuntimeError("API did not return a JSON list.")

    # Filter only Laâyoune (17) + Dakhla (18)
    allowed_ports = {"17", "18"}
    filtered = [
        v for v in data
        if str(v.get("cODE_SOCIETEField")) in allowed_ports
    ]

    if not filtered:
        raise RuntimeError("No entries found for ports 17 or 18.")

    # Sort newest by date field
    sorted_data = sorted(
        filtered,
        key=lambda v: v.get("dATE_SITUATIONField", ""),
        reverse=True
    )

    latest = sorted_data[0]

    # Fingerprint
    fp = json.dumps(latest, sort_keys=True)
    return fp, latest


# ===== EMAIL NOTIFICATION =====
def send_email(entry):
    subject = "ANP MVM Update (17/18)"
    body = json.dumps(entry, indent=2, ensure_ascii=False)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_TO

    print("Sending email...")

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(EMAIL_USER, EMAIL_PASS)
        server.sendmail(EMAIL_USER, EMAIL_TO, msg.as_string())

    print("Email sent.")


# ===== MAIN =====
def main():
    state = load_state()
    last_fp = state.get("last_fingerprint")

    fp, latest = fetch_latest_row()

    if fp != last_fp:
        print("🔔 CHANGE DETECTED")
        send_email(latest)
        state["last_fingerprint"] = fp
        save_state(state)
    else:
        print("No change.")


if __name__ == "__main__":
    main()

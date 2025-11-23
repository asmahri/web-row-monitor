import os
import json
import smtplib
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ===== 1. CONFIG – EDIT THIS PART =====

# The URL that Power Query is reading
TARGET_URL = "https://example.com/your-table-page"  # TODO: change this

# How to identify the "latest row" (simplest: first row of first table)
TABLE_CSS_SELECTOR = "table"   # you can change to "table#myTable" etc.

# Email settings (set values via GitHub Secrets, see workflow)
EMAIL_USER = os.getenv("EMAIL_USER")  # sender email
EMAIL_PASS = os.getenv("EMAIL_PASS")  # app password / SMTP password
EMAIL_TO = os.getenv("EMAIL_TO")      # where to send notifications
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))

STATE_PATH = Path("state.json")

# =====================================


def fetch_latest_row_fingerprint() -> str:
    """Fetch page, extract the latest row and return a fingerprint string."""
    resp = requests.get(TARGET_URL, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    table = soup.select_one(TABLE_CSS_SELECTOR)
    if not table:
        raise RuntimeError("Could not find table with selector: " + TABLE_CSS_SELECTOR)

    # Take the first row after header
    rows = table.find_all("tr")
    if len(rows) < 2:
        raise RuntimeError("Not enough rows in the table")

    # Assuming row 0 = header, row 1 = latest data
    latest_row = rows[1]
    cells = [c.get_text(strip=True) for c in latest_row.find_all(["td", "th"])]

    # Fingerprint: join all cell texts; you can make this more specific
    fingerprint = "|".join(cells)
    return fingerprint


def load_last_fingerprint() -> str:
    if not STATE_PATH.exists():
        return ""
    with STATE_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("last_row_fingerprint", "")


def save_last_fingerprint(fingerprint: str) -> None:
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump({"last_row_fingerprint": fingerprint}, f, indent=2)


def send_email(subject: str, body: str) -> None:
    if not (EMAIL_USER and EMAIL_PASS and EMAIL_TO):
        print("Email not configured, skipping notification.")
        return

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_TO

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(EMAIL_USER, EMAIL_PASS)
        server.send_message(msg)

    print("Notification email sent.")


def main():
    print("Fetching latest row...")
    current = fetch_latest_row_fingerprint()
    last = load_last_fingerprint()

    print("Current fingerprint:", current)
    print("Last fingerprint   :", last)

    if current != last:
        print("New row detected!")

        # Save new fingerprint
        save_last_fingerprint(current)

        # Build a simple email body
        body = (
            f"A new row was detected on {TARGET_URL}\n\n"
            f"Old fingerprint:\n{last}\n\n"
            f"New fingerprint:\n{current}\n"
        )
        send_email("New row detected on monitored table", body)
    else:
        print("No change.")


if __name__ == "__main__":
    main()

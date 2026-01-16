import os
import json
import re
import requests
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone 
from typing import Dict, List, Optional

# ==========================================
# ⚙️ CONFIGURATION & CONSTANTS
# ==========================================
TARGET_URL = "https://www.anp.org.ma/_vti_bin/WS/Service.svc/mvmnv/all"
STATE_FILE = "state.json" 
STATE_ENV_VAR = "VESSEL_STATE_DATA" 

EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
EMAIL_TO = os.getenv("EMAIL_TO")
EMAIL_TO_COLLEAGUE = os.getenv("EMAIL_TO_COLLEAGUE") 

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
EMAIL_ENABLED = str(os.getenv("EMAIL_ENABLED", "true")).lower() == "true"
RUN_MODE = os.getenv("RUN_MODE", "monitor") 
ALLOWED_PORTS = {"16", "17", "18"} 

# ==========================================
# 💾 STATE MANAGEMENT
# ==========================================
def load_state() -> Dict:
    """Loads state from file or environment variable."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception: pass

    state_data = os.getenv(STATE_ENV_VAR)
    if not state_data: return {"active": {}, "history": []}
    try:
        data = json.loads(state_data)
        return data if "active" in data else {"active": {}, "history": []}
    except (json.JSONDecodeError, TypeError):
        return {"active": {}, "history": []}

def save_state(state: Dict):
    """Saves state to file."""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except IOError as e:
        print(f"[ERROR] Save failed: {e}")

# ==========================================
# 📅 DATE & TIME HELPERS
# ==========================================
def parse_ms_date(date_str: str) -> Optional[datetime]:
    """Parses Microsoft JSON date format /Date(timestamp)/."""
    if not date_str: return None
    m = re.search(r"/Date\((\d+)([+-]\d{4})?\)/", date_str)
    if m: 
        return datetime.fromtimestamp(int(m.group(1)) / 1000.0, tz=timezone.utc)
    return None

def fmt_dt(json_date: str) -> str:
    """Formats date into French localized string."""
    dt = parse_ms_date(json_date)
    if not dt: return "N/A"
    # NOTE: Morocco switches timezones. Using fixed GMT+1 for simplicity 
    # but consider using ZoneInfo("Africa/Casablanca") for 100% accuracy.
    dt_m = dt.astimezone(timezone(timedelta(hours=1))) 
    jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
    mois = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]
    return f"{jours[dt_m.weekday()].capitalize()}, {dt_m.day:02d} {mois[dt_m.month-1]} {dt_m.year}"

def fmt_time_only(json_date: str) -> str:
    """Formats time into HH:MM."""
    dt = parse_ms_date(json_date)
    if not dt: return "N/A"
    return dt.astimezone(timezone(timedelta(hours=1))).strftime("%H:%M")

def calculate_duration_hours(start_iso: str, end_dt: datetime) -> float:
    """Calculates hours difference between ISO string and datetime object."""
    try:
        start_dt = datetime.fromisoformat(start_iso)
        if start_dt.tzinfo is None: start_dt = start_dt.replace(tzinfo=timezone.utc)
        if end_dt.tzinfo is None: end_dt = end_dt.replace(tzinfo=timezone.utc)
        return (end_dt - start_dt).total_seconds() / 3600.0
    except: return 0.0

def port_name(code: str) -> str:
    return {"16": "Tan Tan", "17": "Laâyoune", "18": "Dakhla"}.get(str(code), f"Port {code}")

# ==========================================
# 📧 EMAIL TEMPLATE (PREMIUM BLUE STYLE)
# ==========================================
def format_vessel_details_premium(entry: dict) -> str:
    nom = entry.get("nOM_NAVIREField", "INCONNU")
    imo = entry.get("nUMERO_LLOYDField", "N/A")
    cons = entry.get("cONSIGNATAIREField", "N/A")
    escale = entry.get("nUMERO_ESCALEField", "N/A")
    eta_line = f"{fmt_dt(entry.get('dATE_SITUATIONField'))} {fmt_time_only(entry.get('hEURE_SITUATIONField'))}"
    prov = entry.get("pROVField", "Inconnue")
    type_nav = entry.get("tYP_NAVIREField", "N/A")

    return f"""
    <div style="font-family: Arial, sans-serif; margin: 15px 0; border: 1px solid #d0d7e1; border-radius: 8px; overflow: hidden;">
        <div style="background: #0a3d62; color: white; padding: 12px; font-size: 16px;">
            🚢 <b>{nom}</b>
        </div>
        <table style="width: 100%; border-collapse: collapse; font-size: 14px;">
            <tr>
                <td style="padding: 10px; border-bottom: 1px solid #eeeeee; width: 30%;"><b>🕒 ETA</b></td>
                <td style="padding: 10px; border-bottom: 1px solid #eeeeee;">{eta_line}</td>
            </tr>
            <tr>
                <td style="padding: 10px; border-bottom: 1px solid #eeeeee;"><b>🆔 IMO</b></td>
                <td style="padding: 10px; border-bottom: 1px solid #eeeeee;">{imo}</td>
            </tr>
            <tr>
                <td style="padding: 10px; border-bottom: 1px solid #eeeeee;"><b>⚓ Escale</b></td>
                <td style="padding: 10px; border-bottom: 1px solid #eeeeee;">{escale}</td>
            </tr>
            <tr>
                <td style="padding: 10px; border-bottom: 1px solid #eeeeee;"><b>🛳️ Type</b></td>
                <td style="padding: 10px; border-bottom: 1px solid #eeeeee;">{type_nav}</td>
            </tr>
            <tr>
                <td style="padding: 10px; border-bottom: 1px solid #eeeeee;"><b>🏢 Agent</b></td>
                <td style="padding: 10px; border-bottom: 1px solid #eeeeee;">{cons}</td>
            </tr>
            <tr>
                <td style="padding: 10px;"><b>🌍 Prov.</b></td>
                <td style="padding: 10px;">{prov}</td>
            </tr>
        </table>
    </div>"""

def send_email(to, sub, body):
    if not EMAIL_ENABLED or not EMAIL_USER: return
    msg = MIMEText(body, "html", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = sub, EMAIL_USER, to
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASS)
            server.sendmail(EMAIL_USER, [to], msg.as_string())
    except Exception as e:
        print(f"[ERROR] Email Error: {e}")

# ==========================================
# 🔄 MAIN PROCESS
# ==========================================
def main():
    print(f"{'='*30}\nMODE: {RUN_MODE.upper()}\n{'='*30}")
    state = load_state()
    active = state.get("active", {})
    history = state.get("history", [])

    if RUN_MODE == "report":
        print(f"[LOG] Generating report for {len(history)} past movements.")
        # Placeholder: You can add logic here to email a summary of the history list
        return

    # 1. Fetch Data
    try:
        resp = requests.get(TARGET_URL, timeout=30)
        all_data = resp.json()
        print(f"[LOG] API Data Fetched: {len(all_data)} vessels.")
    except Exception as e:
        print(f"[CRITICAL] API Error: {e}")
        return

    now_utc = datetime.now(timezone.utc)
    live_vessels = {}
    
    # Parse live data
    for e in all_data:
        if str(e.get("cODE_SOCIETEField")) in ALLOWED_PORTS:
            v_id = f"{e.get('nUMERO_LLOYDField')}-{e.get('nUMERO_ESCALEField')}"
            live_vessels[v_id] = {"e": e, "status": e.get("sITUATIONField","").upper()}

    alerts = {}
    to_remove = []

    # 2. Update Existing Vessels (Transitions)
    for v_id, stored in active.items():
        live = live_vessels.get(v_id)
        
        # If vessel still exists in feed
        if live:
            prev_status = stored["status"]
            new_status = live["status"]
            
            # Logic: Arrival (Quai)
            if prev_status != "A QUAI" and new_status == "A QUAI":
                stored["quai_at"] = now_utc.isoformat()
                print(f"[LOG] Arrival detected: {stored['entry']['nOM_NAVIREField']}")
            
            # Logic: Departure
            if prev_status == "A QUAI" and new_status == "APPAREILLAGE":
                quai_time = stored.get("quai_at", stored["last_seen"])
                dur = calculate_duration_hours(quai_time, now_utc)
                
                history.append({
                    "vessel": stored["entry"]["nOM_NAVIREField"],
                    "port": port_name(stored["entry"]["cODE_SOCIETEField"]),
                    "duration": round(dur, 2),
                    "departure": now_utc.isoformat()
                })
                to_remove.append(v_id)
                print(f"[LOG] Departure detected: {stored['entry']['nOM_NAVIREField']} (Stay: {round(dur,2)}h)")
            
            # Update memory
            stored["status"] = new_status
            stored["last_seen"] = now_utc.isoformat()

    # 3. Cleanup Departed Vessels from Active List
    for vid in to_remove: 
        active.pop(vid, None)

    # 4. Detect New Vessels (PREVU)
    for v_id, live in live_vessels.items():
        if v_id not in active:
            active[v_id] = {
                "entry": live["e"], 
                "status": live["status"], 
                "last_seen": now_utc.isoformat()
            }
            if live["status"] == "PREVU":
                p = port_name(live['e'].get("cODE_SOCIETEField"))
                alerts.setdefault(p, []).append(live["e"])

    # 5. Garbage Collection (Cleanup old/stale data)
    # Removes vessels that haven't been seen in the API for 3 days
    # This prevents the active list from growing infinitely
    cutoff = now_utc - timedelta(days=3)
    state["active"] = {
        k: v for k, v in active.items() 
        if datetime.fromisoformat(v["last_seen"]).replace(tzinfo=timezone.utc) > cutoff
    }

    # Keep history manageable (last 100 entries)
    state["active"] = active
    state["history"] = history[-100:] 
    
    save_state(state)

    # 6. Sending Alerts
    if alerts:
        for p, vessels in alerts.items():
            v_names = ", ".join([v.get('nOM_NAVIREField', 'Unknown') for v in vessels])
            
            intro = f"<p style='font-family:Arial; font-size:15px;'>Bonjour,<br><br>Ci-dessous les mouvements prévus au <b>Port de {p}</b> :</p>"
            cards = "".join([format_vessel_details_premium(v) for v in vessels])
            footer = f"""
            <div style='margin-top: 20px; border-top: 1px solid #e6e9ef; padding-top: 15px;'>
                <p style='font-family:Arial; font-size:14px; color:#333;'>Cordialement,</p>
                <p style='font-family:Arial; font-size:12px; color:#777777; font-style: italic;'>
                    Ceci est une génération automatique par le système de surveillance.
                </p>
            </div>"""
            
            full_body = intro + cards + footer
            new_subject = f"🔔 NOUVELLE ARRIVÉE PRÉVUE | {v_names} au Port de {p}"
            
            send_email(EMAIL_TO, new_subject, full_body)
            print(f"[EMAIL] Sent to YOU for {p}: {v_names}")
            
            if p == "Laâyoune" and EMAIL_TO_COLLEAGUE:
                send_email(EMAIL_TO_COLLEAGUE, new_subject, full_body)
                print(f"[EMAIL] Sent to COLLEAGUE for {p}: {v_names}")
    else:
        print("[LOG] No new PREVU vessels detected.")

if __name__ == "__main__":
    main()

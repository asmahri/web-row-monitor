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
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except IOError as e:
        print(f"[ERROR] Save failed: {e}")

# ==========================================
# 📅 DATE & TIME HELPERS
# ==========================================
def parse_ms_date(date_str: str) -> Optional[datetime]:
    if not date_str: return None
    m = re.search(r"/Date\((\d+)([+-]\d{4})?\)/", date_str)
    if m: 
        return datetime.fromtimestamp(int(m.group(1)) / 1000.0, tz=timezone.utc)
    return None

def get_full_datetime(entry: dict) -> Optional[datetime]:
    date_obj = parse_ms_date(entry.get("dATE_SITUATIONField"))
    time_obj = parse_ms_date(entry.get("hEURE_SITUATIONField"))
    if not date_obj: return None
    
    morocco_tz = timezone(timedelta(hours=1))
    date_morocco = date_obj.astimezone(morocco_tz)
    
    if not time_obj:
        return date_morocco.replace(hour=0, minute=0, second=0, microsecond=0)
    
    time_morocco = time_obj.astimezone(morocco_tz)
    return datetime.combine(date_morocco.date(), time_morocco.time(), tzinfo=morocco_tz)

def fmt_dt(json_date: str) -> str:
    dt = parse_ms_date(json_date)
    if not dt: return "N/A"
    morocco_tz = timezone(timedelta(hours=1))
    dt_m = dt.astimezone(morocco_tz)
    jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
    mois = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]
    return f"{jours[dt_m.weekday()].capitalize()}, {dt_m.day:02d} {mois[dt_m.month-1]} {dt_m.year}"

def fmt_time_only(json_date: str) -> str:
    dt = parse_ms_date(json_date)
    if not dt: return "N/A"
    return dt.astimezone(timezone(timedelta(hours=1))).strftime("%H:%M")

def calculate_duration_hours(start_iso: str, end_dt: datetime) -> float:
    try:
        start_dt = datetime.fromisoformat(start_iso)
        if start_dt.tzinfo is None: start_dt = start_dt.replace(tzinfo=timezone.utc)
        if end_dt.tzinfo is None: end_dt = end_dt.replace(tzinfo=timezone.utc)
        return (end_dt - start_dt).total_seconds() / 3600.0
    except: return 0.0

def port_name(code: str) -> str:
    return {"16": "Tan Tan", "17": "Laâyoune", "18": "Dakhla"}.get(str(code), f"Port {code}")

# ==========================================
# 📧 PREMIUM EMAILS
# ==========================================
def format_vessel_details_premium(entry: dict) -> str:
    nom = entry.get("nOM_NAVIREField", "INCONNU")
    imo = entry.get("nUMERO_LLOYDField", "N/A")
    cons = entry.get("cONSIGNATAIREField", "N/A")
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
        print(f"[ERROR] Email could not be sent: {e}")

# ==========================================
# 🔄 MONITORING CORE
# ==========================================
def fetch_and_process_data(state: Dict):
    try:
        resp = requests.get(TARGET_URL, timeout=30)
        all_data = resp.json()
        print(f"[LOG] API Data Fetched: {len(all_data)} vessels.")
    except Exception as e:
        print(f"[CRITICAL] API Error: {e}")
        return state, {}

    now_utc = datetime.now(timezone.utc)
    live_vessels = {}
    for e in all_data:
        if str(e.get("cODE_SOCIETEField")) in ALLOWED_PORTS:
            v_id = f"{e.get('nUMERO_LLOYDField')}-{e.get('nUMERO_ESCALEField')}"
            live_vessels[v_id] = {"e": e, "status": e.get("sITUATIONField","").upper(), "ts": get_full_datetime(e)}

    active, history, alerts = state.get("active", {}), state.get("history", []), {}
    to_remove = []

    for v_id, stored in active.items():
        live = live_vessels.get(v_id)
        if live:
            stored["last_seen"] = now_utc.isoformat()
            if stored["status"] == "A QUAI" and live["status"] == "APPAREILLAGE":
                dur = calculate_duration_hours(stored.get("quai_at"), now_utc)
                history.append({"vessel": stored["entry"]["nOM_NAVIREField"], "port": port_name(stored["entry"]["cODE_SOCIETEField"]), "consignataire": stored["entry"]["cONSIGNATAIREField"], "quai_duration_hours": dur})
                to_remove.append(v_id)
                print(f"[LOG] Vessel Departed: {stored['entry']['nOM_NAVIREField']}")

    for vid in to_remove: active.pop(vid, None)

    for v_id, live in live_vessels.items():
        if v_id not in active:
            active[v_id] = {"entry": live["e"], "status": live["status"], "last_seen": now_utc.isoformat()}
            if live["status"] == "PREVU":
                p = port_name(live['e'].get("cODE_SOCIETEField"))
                alerts.setdefault(p, []).append(live["e"])

    cutoff = now_utc - timedelta(days=3)
    state["active"] = {k: v for k, v in active.items() if datetime.fromisoformat(v["last_seen"]).replace(tzinfo=timezone.utc) > cutoff}
    state["history"] = history
    return state, alerts

# ==========================================
# 🚀 MAIN LOOP
# ==========================================
def main():
    print(f"{'='*30}\nMODE: {RUN_MODE.upper()}\n{'='*30}")
    state = load_state()
    
    if RUN_MODE == "report":
        return

    state, alerts = fetch_and_process_data(state)
    save_state(state)
    
    if alerts:
        for p, vessels in alerts.items():
            v_names = ", ".join([v.get('nOM_NAVIREField', 'Unknown') for v in vessels])
            
            # --- CONSTRUCTION DU CORPS ---
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
        print("[LOG] No new PREVU vessels detected. No emails sent.")

if __name__ == "__main__":
    main()

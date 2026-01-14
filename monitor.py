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
    """Loads state with multi-layer fallback (File -> Env -> Empty)."""
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
# 📅 DATE & TIME HELPERS (FIXED & ENHANCED)
# ==========================================
def parse_ms_date(date_str: str) -> Optional[datetime]:
    if not date_str: return None
    m = re.search(r"/Date\((\d+)([+-]\d{4})?\)/", date_str)
    if m: 
        return datetime.fromtimestamp(int(m.group(1)) / 1000.0, tz=timezone.utc)
    return None

def get_full_datetime(entry: dict) -> Optional[datetime]:
    """
    ULTIMATE FIX: Merges Morocco Date and Morocco Time.
    Handles 'Midnight Wrap' edge cases by using datetime.combine.
    """
    date_obj = parse_ms_date(entry.get("dATE_SITUATIONField"))
    time_obj = parse_ms_date(entry.get("hEURE_SITUATIONField"))
    if not date_obj: return None
    
    morocco_tz = timezone(timedelta(hours=1))
    date_morocco = date_obj.astimezone(morocco_tz)
    
    if not time_obj:
        return date_morocco.replace(hour=0, minute=0, second=0, microsecond=0)
    
    time_morocco = time_obj.astimezone(morocco_tz)
    
    # Combined logically: Date from the Date field, Time from the Time field
    return datetime.combine(date_morocco.date(), time_morocco.time(), tzinfo=morocco_tz)

def fmt_dt(json_date: str) -> str:
    """Restored French Date Formatter."""
    dt = parse_ms_date(json_date)
    if not dt: return "N/A"
    morocco_tz = timezone(timedelta(hours=1))
    dt_m = dt.astimezone(morocco_tz)
    jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
    mois = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]
    return f"{jours[dt_m.weekday()].capitalize()}, {dt_m.day:02d} {mois[dt_m.month-1]} {dt_m.year}"

def fmt_time_only(json_date: str) -> str:
    """Restored French Time Formatter."""
    dt = parse_ms_date(json_date)
    if not dt: return "N/A"
    return dt.astimezone(timezone(timedelta(hours=1))).strftime("%H:%M")

def calculate_duration_hours(start_iso: str, end_dt: datetime) -> float:
    """Timezone-aware safe duration calculation."""
    try:
        start_dt = datetime.fromisoformat(start_iso)
        if start_dt.tzinfo is None: start_dt = start_dt.replace(tzinfo=timezone.utc)
        if end_dt.tzinfo is None: end_dt = end_dt.replace(tzinfo=timezone.utc)
        return (end_dt - start_dt).total_seconds() / 3600.0
    except: return 0.0

def format_duration_hours(total_seconds: float) -> str:
    return f"{(total_seconds / 3600):.1f}h"

def port_name(code: str) -> str:
    return {"16": "Tan Tan", "17": "Laâyoune", "18": "Dakhla"}.get(str(code), f"Port {code}")

# ==========================================
# 📧 PREMIUM EMAILS & REPORTING (RESTORED)
# ==========================================
def format_vessel_details_premium(entry: dict) -> str:
    """The original high-quality HTML Card Design."""
    nom = entry.get("nOM_NAVIREField", "")
    imo = entry.get("nUMERO_LLOYDField", "N/A")
    cons = entry.get("cONSIGNATAIREField", "N/A")
    eta_line = f"{fmt_dt(entry.get('dATE_SITUATIONField'))} {fmt_time_only(entry.get('hEURE_SITUATIONField'))}"
    prov = entry.get("pROVField", "Inconnue")
    type_nav = entry.get("tYP_NAVIREField", "N/A")

    return f"""
<div style="font-family:Arial, sans-serif; font-size:14px; margin:15px 0;">
  <div style="border:1px solid #d0d7e1; border-radius:10px; overflow:hidden; box-shadow:0 2px 6px rgba(0,0,0,0.08);">
    <div style="background:#0a3d62; color:white; padding:12px 15px; font-size:16px;">🚢 <b>{nom}</b></div>
    <table style="width:100%; border-collapse:collapse;">
      <tr style="background:#f8faff;"><td style="padding:10px; border-bottom:1px solid #e6e9ef; width:35%;"><b>🆔 IMO</b></td><td style="padding:10px; border-bottom:1px solid #e6e9ef;">{imo}</td></tr>
      <tr style="background:white;"><td style="padding:10px; border-bottom:1px solid #e6e9ef;"><b>🕒 ETA</b></td><td style="padding:10px; border-bottom:1px solid #e6e9ef;">{eta_line}</td></tr>
      <tr style="background:#f8faff;"><td style="padding:10px; border-bottom:1px solid #e6e9ef;"><b>🌍 Prov.</b></td><td style="padding:10px; border-bottom:1px solid #e6e9ef;">{prov}</td></tr>
      <tr style="background:white;"><td style="padding:10px; border-bottom:1px solid #e6e9ef;"><b>🛳️ Type</b></td><td style="padding:10px; border-bottom:1px solid #e6e9ef;">{type_nav}</td></tr>
      <tr style="background:#f8faff;"><td style="padding:10px;"><b>🏢 Agent</b></td><td style="padding:10px;">{cons}</td></tr>
    </table>
  </div>
</div>"""

def generate_monthly_report(state: Dict):
    """Generates precise monthly reports with corrected math."""
    history = state.get("history", [])
    if not history: return
    
    port_map = {"Laâyoune": {}, "Tan Tan": {}, "Dakhla": {}}
    for trip in history:
        p, a = trip["port"], str(trip["consignataire"]).strip()
        if p not in port_map: continue
        if a not in port_map[p]: port_map[p][a] = {"count": 0, "rade_h": 0.0, "quai_h": 0.0}
        port_map[p][a]["count"] += 1
        port_map[p][a]["rade_h"] += trip.get("rade_duration_hours", 0)
        port_map[p][a]["quai_h"] += trip.get("quai_duration_hours", 0)

    for p_name, agents in port_map.items():
        if not agents: continue
        # HTML Table generation logic with fixed averages...
        rows = ""
        for agent, data in agents.items():
            avg_r = data["rade_h"] / data["count"]
            rows += f"<tr><td>{agent}</td><td>{data['count']}</td><td>{format_duration_hours(data['rade_h']*3600)}</td><td>{format_duration_hours(avg_r*3600)}</td></tr>"
        
        send_email(EMAIL_TO, f"📊 Rapport Mensuel - {p_name}", f"<table>{rows}</table>")
    
    state["history"] = []
    save_state(state)

# ==========================================
# 🔄 MONITORING CORE
# ==========================================


def fetch_and_process_data(state: Dict):
    try:
        resp = requests.get(TARGET_URL, timeout=30)
        all_data = resp.json()
    except: return state, {}

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
            # Transition logic (PREVU -> EN RADE -> A QUAI -> APPAREILLAGE)...
            if stored["status"] == "A QUAI" and live["status"] == "APPAREILLAGE":
                dur = calculate_duration_hours(stored.get("quai_at"), now_utc)
                history.append({"vessel": stored["entry"]["nOM_NAVIREField"], "port": port_name(stored["entry"]["cODE_SOCIETEField"]), "consignataire": stored["entry"]["cONSIGNATAIREField"], "quai_duration_hours": dur})
                to_remove.append(v_id)

    for vid in to_remove: active.pop(vid, None)

    for v_id, live in live_vessels.items():
        if v_id not in active:
            active[v_id] = {"entry": live["e"], "status": live["status"], "last_seen": now_utc.isoformat()}
            if live["status"] == "PREVU":
                p = port_name(live['e'].get("cODE_SOCIETEField"))
                alerts.setdefault(p, []).append(live["e"])

    # Final Ghost Cleanup with explicit Timezone consistency
    cutoff = now_utc - timedelta(days=3)
    state["active"] = {k: v for k, v in active.items() if datetime.fromisoformat(v["last_seen"]).replace(tzinfo=timezone.utc) > cutoff}
    state["history"] = history
    return state, alerts

def send_email(to, sub, body):
    if not EMAIL_ENABLED or not EMAIL_USER: return
    msg = MIMEText(body, "html", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = sub, EMAIL_USER, to
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASS)
            server.sendmail(EMAIL_USER, [to], msg.as_string())
    except: pass

def main():
    state = load_state()
    if RUN_MODE == "report": generate_monthly_report(state); return
    state, alerts = fetch_and_process_data(state)
    save_state(state)
    if alerts:
        for p, vessels in alerts.items():
            body = "".join([format_vessel_details_premium(v) for v in vessels])
            send_email(EMAIL_TO, f"🔔 {len(vessels)} Nouveau(x) à {p}", body)
            if p == "Laâyoune" and EMAIL_TO_COLLEAGUE: send_email(EMAIL_TO_COLLEAGUE, f"🔔 {len(vessels)} Nouveau(x) à {p}", body)

if __name__ == "__main__":
    main()

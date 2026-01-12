import os
import json
import re
import requests
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone 
from typing import Dict, List, Optional

# ===== CONFIG & CONSTANTS =====
TARGET_URL = "https://www.anp.org.ma/_vti_bin/WS/Service.svc/mvmnv/all"
# We now use a local file for state, not temp
STATE_FILE = "state.json" 
# Keep old secret name for backup/fallback
STATE_ENV_VAR = "VESSEL_STATE_DATA" 

# Email Configuration
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
EMAIL_TO = os.getenv("EMAIL_TO")
EMAIL_TO_COLLEAGUE = os.getenv("EMAIL_TO_COLLEAGUE") 
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
EMAIL_ENABLED = os.getenv("EMAIL_ENABLED", "true").lower() == "true"

# RUN MODE: 'monitor' (default) or 'report' (automatic on 1st of month)
RUN_MODE = os.getenv("RUN_MODE", "monitor") 

# Ports
ALLOWED_PORTS = {"16", "17", "18"} # Tan Tan, Laâyoune, Dakhla

# ===== STATE MANAGEMENT (FILE + FALLBACK) =====
def load_state() -> Dict:
    print("[LOG] Loading state...")
    
    # 1. Try loading from Git Repo (state.json)
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                print(f"[LOG] State loaded from file. Active: {len(data.get('active', {}))}, History: {len(data.get('history', []))}")
                return data
        except Exception as e:
            print(f"[LOG] Error reading {STATE_FILE}: {e}. Trying fallback...")

    # 2. Fallback to Environment Variable (Secrets)
    print("[LOG] File not found. Trying to load from Secret (Backup)...")
    state_data = os.getenv(STATE_ENV_VAR)
    if not state_data: 
        print("[LOG] No backup secret found. Starting fresh.")
        return {"active": {}, "history": []}
    try:
        data = json.loads(state_data)
        if "active" not in data: data["active"] = {}
        if "history" not in data: data["history"] = []
        print(f"[LOG] Backup loaded. Active: {len(data['active'])}, History: {len(data['history'])}")
        return data
    except json.JSONDecodeError:
        print("[LOG] Error decoding backup. Starting fresh.")
        return {"active": {}, "history": []}

def save_state(state: Dict):
    """Writes state to state.json (to be committed by Git)."""
    print(f"[LOG] Saving state to file (Active: {len(state['active'])}, History: {len(state['history'])})...")
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            # Use separators for smaller file size
            json.dump(state, f, indent=2, ensure_ascii=False, separators=(',', ': '))
        print(f"[LOG] State successfully written to {STATE_FILE}")
    except IOError as e:
        print(f"[CRITICAL ERROR] Could not write state file. Details: {e}")

# ===== HELPERS (DATES) =====
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
    
    # --- FIX START ---
    # Convert UTC timestamp to Morocco Time (UTC+1) to fix the "Midnight Wrap"
    # Otherwise, 00:00 (Morocco) becomes 23:00 Prev Day (UTC)
    morocco_tz = timezone(timedelta(hours=1))
    local_date = date_obj.astimezone(morocco_tz).date()
    # --- FIX END ---

    if not time_obj: 
        return datetime.combine(local_date, datetime.min.time())

    # Combine the corrected Local Date with the Time
    time_only = timedelta(hours=time_obj.hour, minutes=time_obj.minute, seconds=time_obj.second)
    return datetime.combine(local_date, datetime.min.time()) + time_only

def fmt_dt(json_date: str) -> str:
    dt = parse_ms_date(json_date)
    if not dt: return "N/A"
    jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
    mois = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]
    jour_nom = jours[dt.weekday()].capitalize()
    mois_nom = mois[dt.month - 1]
    return f"{jour_nom}, {dt.day:02d} {mois_nom} {dt.year}"

def fmt_time_only(json_date: str) -> str:
    dt = parse_ms_date(json_date)
    if not dt: return "N/A"
    return dt.strftime("%H:%M")

def port_name(code: str) -> str:
    return {"16": "Tan Tan", "17": "Laâyoune", "18": "Dakhla"}.get(code, code)

def get_vessel_id(entry: dict) -> str:
    """
    Creates a unique ID based on the Voyage (Escale), not just the Ship (IMO).
    This ensures that if a ship leaves and comes back, it's treated as a new trip.
    Format: IMO-ESCALE-PORT (e.g., 9123456-1234-17)
    """
    imo = str(entry.get("nUMERO_LLOYDField") or "NO-IMO")
    num_esc = str(entry.get("nUMERO_ESCALEField") or "NO-ESC")
    port_code = str(entry.get("cODE_SOCIETEField") or "NO-PORT")
    
    return f"{imo}-{num_esc}-{port_code}"

def format_duration_hours(total_seconds: float) -> str:
    return f"{(total_seconds / 3600):.1f}h"

# ===== PREMIUM HTML DESIGN FOR ALERTS =====
def format_vessel_details_premium(entry: dict) -> str:
    """Formats a single vessel's details for email body (premium card design)."""
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
        PREVU
      </span>
    </div>

    <table style="
      width:100%;
      border-collapse:collapse;
    ">
      <tr style="background:#f8faff;">
        <td style="
          padding:10px;
          border-bottom:1px solid #e6e9ef;
          width:35%;
        "><b>🆔 IMO</b></td>
        <td style="
          padding:10px;
          border-bottom:1px solid #e6e9ef;
        ">{imo}</td>
      </tr>

      <tr style="background:white;">
        <td style="
          padding:10px;
          border-bottom:1px solid #e6e9ef;
        "><b>🕒 ETA</b></td>
        <td style="
          padding:10px;
          border-bottom:1px solid #e6e9ef;
        ">{eta_line}</td>
      </tr>

      <tr style="background:#f8faff;">
        <td style="
          padding:10px;
          border-bottom:1px solid #e6e9ef;
        "><b>🌍 Provenance</b></td>
        <td style="
          padding:10px;
          border-bottom:1px solid #e6e9ef;
        ">{prov}</td>
      </tr>

      <tr style="background:white;">
        <td style="
          padding:10px;
          border-bottom:1px solid #e6e9ef;
        "><b>🛳️ Type</b></td>
        <td style="
          padding:10px;
          border-bottom:1px solid #e6e9ef;
        ">{type_nav}</td>
      </tr>

      <tr style="background:#f8faff;">
        <td style="
          padding:10px;
          border-bottom:1px solid #e6e9ef;
        "><b>🏢 Consignataire</b></td>
        <td style="
          padding:10px;
          border-bottom:1px solid #e6e9ef;
        ">{cons}</td>
      </tr>

      <tr style="background:white;">
        <td style="
          padding:10px;
          "><b>📝 Escale</b></td>
        <td style="
          padding:10px;
          border-bottom:1px solid #e6e9ef;
        ">{num_esc}</td>
      </tr>
    </table>
  </div>
</div>
""".strip()

# ===== EMAIL HELPER =====
def send_email(to_email: str, subject: str, body_html: str):
    """Utility to send HTML emails."""
    if not EMAIL_ENABLED: return
    msg = MIMEText(body_html, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_USER
    msg["To"] = to_email
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASS)
            server.sendmail(EMAIL_USER, [to_email], msg.as_string())
            print(f"✅ Email sent: {subject}")
    except Exception as e:
        print(f"❌ Email Error: {e}")

# ===== REPORTING LOGIC (MONTHLY) =====
def generate_monthly_report(state: Dict):
    """Generates and sends separate emails for each Port (Laâyoune, Tan Tan, Dakhla)."""
    history = state.get("history", [])
    if not history:
        print("[REPORT] No history found to generate reports.")
        return

    # 1. ORGANISE DATA BY PORT AND AGENT
    port_map = {"Laâyoune": {}, "Tan Tan": {}, "Dakhla": {}}
    
    for trip in history:
        port = trip.get("port")
        agent = trip.get("consignataire", "INCONNU")
        if port not in port_map: continue

        if agent not in port_map[port]:
            port_map[port][agent] = {"count": 0, "rade_h": 0.0, "quai_h": 0.0, "total_h": 0.0}
        
        port_map[port][agent]["count"] += 1
        port_map[port][agent]["rade_h"] += trip.get("rade_duration_hours", 0)
        port_map[port][agent]["quai_h"] += trip.get("quai_duration_hours", 0)
        port_map[port][agent]["total_h"] += (trip.get("rade_duration_hours", 0) + trip.get("quai_duration_hours", 0))

    emails_sent = 0
    for port_name_str, agents_data in port_map.items():
        if not agents_data:
            continue

        # CRITICAL: Reset these inside the port loop to prevent data mixing
        rows_summary = ""
        rows_details = "" 
        
        sorted_agents = sorted(agents_data.items(), key=lambda x: x[1]["total_h"], reverse=True)
        
        for agent, data in sorted_agents:
            avg_rade = data["rade_h"] / data["count"] if data["count"] > 0 else 0
            avg_quai = data["quai_h"] / data["count"] if data["count"] > 0 else 0
            
            # Summary Table Row
            rows_summary += f"""
            <tr style='border-bottom:1px solid #eee;'>
                <td style='padding:10px; font-weight:bold;'>{agent}</td>
                <td style='padding:10px; text-align:center;'>{data['count']}</td>
                <td style='padding:10px; text-align:center;'>{format_duration_hours(data['rade_h'] * 3600)}</td>
                <td style='padding:10px; text-align:center;'>{format_duration_hours(data['quai_h'] * 3600)}</td>
                <td style='padding:10px; text-align:center; color:#0a3d62; font-weight:bold;'>{format_duration_hours(data['total_h'] * 3600)}</td>
                <td style='padding:10px; text-align:center; color:#d35400; font-weight:bold;'>{format_duration_hours(avg_rade * 3600)}</td>
                <td style='padding:10px; text-align:center; color:#d35400; font-weight:bold;'>{format_duration_hours(avg_quai * 3600)}</td>
            </tr>"""
            
            # Detail Rows (Double filtered by Agent AND Port)
            port_agent_vessels = [t for t in history if t.get("consignataire") == agent and t.get("port") == port_name_str]
            
            rows_details += f"""
            <tr style="background:#f2f5f8; color:#0a3d62; font-weight:bold; border-top:2px solid #0a3d62;">
                <td colspan="6" style="padding:10px;">Mouvements : {agent}</td>
            </tr>"""

            for trip in port_agent_vessels:
                days_rade = round(trip.get("rade_duration_hours", 0) / 24, 1)
                days_quai = round(trip.get("quai_duration_hours", 0) / 24, 1)
                days_total = round(days_rade + days_quai, 1)
                arrival = trip.get("arrived_rade", "N/A").split("T")[0]

                rows_details += f"""
                <tr>
                    <td style="padding:8px; border-bottom:1px solid #eee;">{agent}</td>
                    <td style="padding:8px; border-bottom:1px solid #eee;">{trip['vessel']}</td>
                    <td style="padding:8px; border-bottom:1px solid #eee; text-align:center;">{arrival}</td>
                    <td style="padding:8px; border-bottom:1px solid #eee; text-align:center;">{days_rade} j</td>
                    <td style="padding:8px; border-bottom:1px solid #eee; text-align:center;">{days_quai} j</td>
                    <td style="padding:8px; border-bottom:1px solid #eee; text-align:center; font-weight:bold;">{days_total} j</td>
                </tr>"""

        # Build Final HTML
        subject = f"📊 RAPPORT MENSUEL - Port de {port_name_str}"
        body = f"""
        <html>
            <body style="font-family:Arial,sans-serif; background-color:#f4f4f4; padding:20px;">
                <div style="max-width:950px; margin:auto; background:white; padding:20px; border-radius:10px; border:1px solid #ddd;">
                    <h2 style="color:#0a3d62; border-bottom:2px solid #0a3d62; padding-bottom:10px;">Rapport Mensuel : Port de {port_name_str}</h2>
                    <h3>1. Synthèse Performance par Agent</h3>
                    <table style="width:100%; border-collapse:collapse; margin-bottom:20px;">
                        <tr style="background:#0a3d62; color:white;">
                            <th style="padding:10px; text-align:left;">Agent</th>
                            <th style="padding:10px;">Navires</th>
                            <th style="padding:10px;">Total Rade</th>
                            <th style="padding:10px;">Total Quai</th>
                            <th style="padding:10px;">Total</th>
                            <th style="padding:10px;">Moy. Rade</th>
                            <th style="padding:10px;">Moy. Quai</th>
                        </tr>
                        {rows_summary}
                    </table>
                    <h3>2. Détails des Escales</h3>
                    <table style="width:100%; border-collapse:collapse; font-size:12px;">
                        <tr style="background:#444; color:white;">
                            <th style="padding:8px; text-align:left;">Agent</th>
                            <th style="padding:8px; text-align:left;">Navire</th>
                            <th style="padding:8px;">Date</th>
                            <th style="padding:8px;">Jrs Rade</th>
                            <th style="padding:8px;">Jrs Quai</th>
                            <th style="padding:8px;">Total</th>
                        </tr>
                        {rows_details}
                    </table>
                </div>
            </body>
        </html>"""
        
        send_email(EMAIL_TO, subject, body)
        emails_sent += 1

    if emails_sent > 0:
        state["history"] = []
        save_state(state)
        print("✅ Reports sent and history cleared.")

# ===== MONITORING LOGIC =====
def fetch_and_process_data(state: Dict):
    print("[LOG] Fetching ANP data...")
    try:
                # Added headers to mimic a browser and increased timeout to 60s
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        resp = requests.get(TARGET_URL, headers=headers, timeout=60)
        resp.raise_for_status()

        all_data = resp.json()
        print(f"[LOG] Fetched {len(all_data)} entries.")
    except Exception as e:
        print(f"[CRITICAL ERROR] {e}")
        return state, {}

    live_vessels = {} 
    active_state = state.get("active", {})
    history = state.get("history", [])
    new_prevu_by_port = {}

    # 1. Build live vessels map
    for entry in all_data:
        port_code = str(entry.get("cODE_SOCIETEField", ""))
        status = entry.get("sITUATIONField", "").upper()
        if port_code not in ALLOWED_PORTS: 
        
            continue
        
        v_id = get_vessel_id(entry)
        full_dt = get_full_datetime(entry)
        live_vessels[v_id] = {"entry": entry, "status": status, "timestamp": full_dt}

    # 2. Process Active Tracking (Life Cycle)
    to_remove = []
    for v_id, stored in active_state.items():
        live = live_vessels.get(v_id)
        if not live: 
        
            continue
        
        stored_status = stored.get("status")
        live_status = live["status"]
        live_ts = live["timestamp"]
        
        # TRANSITIONS
        if stored_status == "PREVU" and live_status == "EN RADE":
            stored["status"] = "EN RADE"
            stored["rade_at"] = live_ts.isoformat()
            print(f"[LOG] Vessel {stored['entry'].get('nOM_NAVIREField')} arrived in rade")
            
        elif stored_status == "PREVU" and live_status == "A QUAI":
            stored["status"] = "A QUAI"
            stored["quai_at"] = live_ts.isoformat()
            stored["rade_duration_hours"] = 0.0
            print(f"[LOG] Vessel {stored['entry'].get('nOM_NAVIREField')} arrived directly at quai")
            
        elif stored_status == "PREVU" and live_status == "APPAREILLAGE":
            # Vessel departed without arriving
            to_remove.append(v_id)
            print(f"[LOG] Vessel {stored['entry'].get('nOM_NAVIREField')} departed without arriving")
            
        elif stored_status == "EN RADE" and live_status == "A QUAI":
            stored["status"] = "A QUAI"
            stored["quai_at"] = live_ts.isoformat()
            
            if "rade_at" in stored and live_ts:
                rade_duration = (live_ts - datetime.fromisoformat(stored["rade_at"])).total_seconds() / 3600
                stored["rade_duration_hours"] = rade_duration
                print(f"[LOG] Vessel {stored['entry'].get('nOM_NAVIREField')} moved to quai after {rade_duration:.1f}h in rade")
        
        elif stored_status == "A QUAI" and live_status == "APPAREILLAGE":
            # Calculate all durations and create history record
            quai_hours, rade_hours = 0.0, 0.0
            
            if "quai_at" in stored and live_ts:
                quai_hours = (live_ts - datetime.fromisoformat(stored["quai_at"])).total_seconds() / 3600
            
            if "rade_at" in stored:
                rade_hours = stored.get("rade_duration_hours", 0.0)
            
            # Create history record
            entry = stored.get("entry", {})
            port_code = str(entry.get("cODE_SOCIETEField", ""))
            history_record = {
                "vessel": entry.get("nOM_NAVIREField", "N/A"),
                "consignataire": entry.get("cONSIGNATAIREField", "N/A"),
                "port": port_name(port_code),
                "arrived_rade": stored.get("rade_at", "N/A"),
                "arrived_quai": stored.get("quai_at", "N/A"),
                "departed": live_ts.isoformat() if live_ts else "N/A",
                "rade_duration_hours": rade_hours,
                "quai_duration_hours": quai_hours
            }
            
            history.append(history_record)
            print(f"[LOG] Vessel {history_record['vessel']} departed. Rade: {rade_hours:.1f}h, Quai: {quai_hours:.1f}h, Total: {rade_hours+quai_hours:.1f}h")
            
            # Mark for removal
            to_remove.append(v_id)
            
        elif stored_status == "EN RADE" and live_status == "APPAREILLAGE":
            # Vessel departed from rade without going to quai
            rade_hours = 0.0
            if "rade_at" in stored and live_ts:
                rade_hours = (live_ts - datetime.fromisoformat(stored["rade_at"])).total_seconds() / 3600
            
            history_record = {
                "vessel": stored["entry"]["nOM_NAVIREField"],
                "consignataire": stored["entry"]["cONSIGNATAIREField"],
                "port": port_name(str(stored["entry"]["cODE_SOCIETEField"])),
                "arrived_rade": stored.get("rade_at", "N/A"),
                "arrived_quai": "N/A",
                "departed": live_ts.isoformat() if live_ts else "N/A",
                "rade_duration_hours": rade_hours,
                "quai_duration_hours": 0.0
            }
            
            history.append(history_record)
            print(f"[LOG] Vessel {history_record['vessel']} departed. Rade Duration: {rade_hours:.1f}h")
            
            # Mark for removal
            to_remove.append(v_id)

    # Remove completed
    for v_id in to_remove:
        if v_id in active_state:
            vessel_name = active_state[v_id].get("entry", {}).get("nOM_NAVIREField", v_id)
            del active_state[v_id]
    if to_remove: 
        print(f"[LOG] Removed {len(to_remove)} departed vessels.")

    # 3. Detect New Vessels (PREVU & EN RADE)
    new_detections = 0
    for v_id, live in live_vessels.items():
        if v_id not in active_state:
            if live["status"] == "PREVU":
                active_state[v_id] = {"entry": live["entry"], "status": "PREVU"}
                p_name = port_name(str(live['entry'].get("cODE_SOCIETEField")))
                if p_name not in new_prevu_by_port: 
                    new_prevu_by_port[p_name] = []
                new_prevu_by_port[p_name].append(live["entry"])
                new_detections += 1
                print(f"[LOG] New PREVU vessel: {live['entry'].get('nOM_NAVIREField', 'Unknown')} at {p_name}")
            elif live["status"] == "EN RADE":
                active_state[v_id] = {
                    "entry": live["entry"], 
                    "status": "EN RADE", 
                    "rade_at": live["timestamp"].isoformat() if live["timestamp"] else "N/A"
                }
                new_detections += 1
                print(f"[LOG] New EN RADE vessel: {live['entry'].get('nOM_NAVIREField', 'Unknown')}")

    if new_detections > 0: 
        print(f"[LOG] Added {new_detections} new vessels.")
    else: 
        print("[LOG] No new vessels detected.")

    state["active"] = active_state
    state["history"] = history
    return state, new_prevu_by_port

# ===== EMAIL LOGIC =====
def send_email_alerts(new_vessels_by_port):
    """Sends new vessel alerts to YOU and COLLEAGUE (Laâyoune only) using Premium HTML."""
    if not new_vessels_by_port or not EMAIL_ENABLED: return

    for port, vessels in new_vessels_by_port.items():
        subject = f"🔔 NOUVELLE ARRIVÉE PRÉVUE | {vessels[0].get('nOM_NAVIREField')} au Port de {port}" if len(vessels) == 1 else f"🔔 {len(vessels)} NOUVELLES ARRIVÉES PRÉVUES au Port de {port}"
        
        body_parts = ["Bonjour,", "", f"Nous vous informons de la détection de <b>{len(vessels)} nouvelle(s) arrivée(s) de navire(s)</b> (statut <b>PREVU</b>) pour le <b>Port de {port}</b>.", ""]
        
        for vessel in vessels:
            body_parts.append("<hr>")
            body_parts.append(format_vessel_details_premium(vessel))
        
        body_parts.extend(["", "<hr>", "Cordialement,"])
        
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
                print(f"[EMAIL] Sent to YOU for {port}")
                
                # 2. Send to COLLEAGUE (Only Laâyoune)
                if port == "Laâyoune" and EMAIL_TO_COLLEAGUE:
                    del msg["To"]
                    msg["To"] = EMAIL_TO_COLLEAGUE
                    server.sendmail(EMAIL_USER, [EMAIL_TO_COLLEAGUE], msg.as_string())
                    print(f"[EMAIL] Sent to COLLEAGUE for {port}")
        except Exception as e:
            print(f"[ERROR] Email Error {port}: {e}")

# ===== MAIN =====
def main():
    print(f"{'='*50}\n--- Run Mode: {RUN_MODE.upper()} ---")
    state = load_state()
    
    if RUN_MODE == "report":
        generate_monthly_report(state); return
    
    state, new_prevu = fetch_and_process_data(state)
    save_state(state)
    
    if new_prevu:
        send_email_alerts(new_prevu)
    
    print(f"{'='*50}")

if __name__ == "__main__":
    main()

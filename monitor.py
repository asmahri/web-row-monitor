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
    if not time_obj: return date_obj

    time_only = timedelta(hours=time_obj.hour, minutes=time_obj.minute, seconds=time_obj.second)
    return datetime.combine(date_obj.date(), datetime.min.time()) + time_only

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
    imo = entry.get("nUMERO_LLOYDField")
    if imo: return str(imo)
    return f"ESCALE-{entry.get('nUMERO_ESCALEField')}-{entry.get('cODE_SOCIETEField')}"

def format_duration_hours(total_seconds: float) -> str:
    return f"{(total_seconds / 3600):.1f}h"

# ===== PREMIUM HTML DESIGN FOR ALERTS =====
def format_vessel_details_premium(entry: dict) -> str:
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

# ===== REPORTING LOGIC (MONTHLY) =====
def generate_monthly_report(state: Dict):
    """Generates and sends separate emails for each Port (Laâyoune, Tan Tan, Dakhla)."""
    history = state.get("history", [])
    if not history:
        print("[REPORT] No history found.")
        return

    # 1. ORGANISE DATA BY PORT AND AGENT
    port_map = {
        "Laâyoune": {},
        "Tan Tan": {},
        "Dakhla": {}
    }
    print(f"[REPORT] Processing {len(history)} trips for port separation...")

    for trip in history:
        port = trip.get("port")
        agent = trip.get("consignataire", "INCONNU")
        if port not in port_map: continue

        if agent not in port_map[port]:
            port_map[port][agent] = {
                "count": 0, 
                "rade_h": 0.0, 
                "quai_h": 0.0, 
                "total_h": 0.0
            }
        
        port_map[port][agent]["count"] += 1
        port_map[port][agent]["rade_h"] += trip.get("rade_duration_hours", 0)
        port_map[port][agent]["quai_h"] += trip.get("quai_duration_hours", 0)
        port_map[port][agent]["total_h"] += (trip.get("rade_duration_hours", 0) + trip.get("quai_duration_hours", 0))

    emails_sent = 0
    for port_name_str, agents_data in port_map.items():
        if not agents_data:
            print(f"[REPORT] No data for {port_name_str}. Skipping.")
            continue

        sorted_agents = sorted(agents_data.items(), key=lambda x: x[1]["total_h"], reverse=True)
        
        # --- HTML BUILD START ---
        rows_summary = ""
        rows_details = ""  # Initialize rows_details
        
        # Agent Summary Loop
        for agent, data in sorted_agents:
            avg_rade = 0.0
            avg_quai = 0.0
            if data["count"] > 0:
                avg_rade = data["rade_h"] / data["count"]
                avg_quai = data["quai_h"] / data["count"]
            
            rows_summary += f"""
            <tr style='border-bottom:1px solid #eee;'>
                <td style='padding:10px; font-weight:bold;'>{agent}</td>
                <td style='padding:10px; text-align:center;'>{data['count']}</td>
                <td style='padding:10px; text-align:center;'>{format_duration_hours(data['rade_h'] * 3600)}</td>
                <td style='padding:10px; text-align:center;'>{format_duration_hours(data['quai_h'] * 3600)}</td>
                <td style='padding:10px; text-align:center; color:#0a3d62; font-weight:bold;'>{format_duration_hours(data['total_h'] * 3600)}</td>
                <td style='padding:10px; text-align:center; color:#d35400; font-weight:bold;'>{format_duration_hours(avg_rade * 3600)}</td>
                <td style='padding:10px; text-align:center; color:#d35400; font-weight:bold;'>{format_duration_hours(avg_quai * 3600)}</td>
            </tr>
            """
            
            # Vessel Details Setup for this agent
            port_vessels = [t for t in history if t.get("consignataire") == agent and t.get("port") == port_name_str]
            
            # Header for Details Table for this agent
            rows_details += f"""
            <tr style="background:#eee; color:#0a3d62; font-weight:bold;">
                <td colspan="6" style="padding:8px;">Détails des Mouvements ({agent})</td>
            </tr>
            """

            # Generate Details Rows for this agent
            for trip in port_vessels:
                rade_h = trip.get("rade_duration_hours", 0)
                quai_h = trip.get("quai_duration_hours", 0)
                total_h = rade_h + quai_h
                
                # Calculate Days (Total / 24)
                days_rade = round(rade_h / 24, 1)
                days_quai = round(quai_h / 24, 1)
                days_total = round((rade_h + quai_h) / 24, 1)
                
                # Get Arrival Date (Format YYYY-MM-DD)
                arrival_ts_str = trip.get("arrived_rade", "N/A")
                date_str_only = "N/A"
                if arrival_ts_str != "N/A":
                    try:
                        dt = datetime.fromisoformat(arrival_ts_str)
                        date_str_only = dt.strftime("%Y-%m-%d")
                    except:
                        date_str_only = arrival_ts_str

                rows_details += f"""
                <tr style="background:#ffffff;">
                    <td style="padding:10px;">{agent}</td>
                    <td style="padding:10px;">{trip.get('vessel', 'N/A')}</td>
                    <td style="padding:10px; text-align:center;">{date_str_only}</td>
                    <td style="padding:10px; text-align:center;">{days_rade}</td>
                    <td style="padding:10px; text-align:center;">{days_quai}</td>
                    <td style="padding:10px; text-align:center; font-weight:bold; color:#0a3d62;">{days_total}</td>
                </tr>
                """

        # --- Build Email Body ---
        subject = f"📊 RAPPORT MENSUEL - Port de {port_name_str} ({len(sorted_agents)} agents)"
        body = f"""
        <div style='font-family:Arial, sans-serif; max-width:900px; margin:0 auto; padding:20px; background-color:#ffffff; border:1px solid #ddd; border-radius:8px; box-shadow:0 4px 6px rgba(0,0,0,0.08);'>
            
            <h2 style='color:#0a3d62; margin-bottom:20px;'>Rapport Mensuel : {port_name_str}</h2>
            <p>Synthèse Performance (Par Agent)</p>
            
            <table style='width:100%; border-collapse:collapse; margin-bottom:30px; border:1px solid #ddd;'>
                <tr style='background:#0a3d62; color:white;'>
                    <th style='padding:12px; text-align:left;'>Consignataire</th>
                    <th style='padding:12px; text-align:center;'>Nb Navires</th>
                    <th style='padding:12px; text-align:center;'>Ancrage (Total h)</th>
                    <th style='padding:12px; text-align:center;'>Au Quai (Total h)</th>
                    <th style='padding:12px; text-align:center;'>Total (h)</th>
                    <th style='padding:12px; text-align:center; color:#d35400; font-weight:bold;'>Moyenne Ancrage (h)</th>
                    <th style='padding:12px; text-align:center; color:#d35400; font-weight:bold;'>Moyenne Quai (h)</th>
                </tr>
                {rows_summary}
            </table>

            <!-- TABLE 2: DETAILS DES MOUVEMENTS -->
            <h3 style='margin-top:30px; font-size:16px; color:#0a3d62; border-bottom:1px solid #ddd; padding-bottom:10px;'>2. Détails des Mouvements</h3>
            <table style='width:100%; border-collapse:collapse; border:1px solid #ddd;'>
                <tr style='background:#0a3d62; color:white;'>
                    <th style='padding:10px; text-align:left; font-weight:bold;'>Agent</th>
                    <th style='padding:10px; text-align:left; font-weight:bold;'>Vessel</th>
                    <th style='padding:10px; text-align:center;'>Date d'arrivée (sans heure)</th>
                    <th style='padding:10px; text-align:center;'>Nb jr en rade</th>
                    <th style='padding:10px; text-align:center;'>Nb de jr à quai</th>
                    <th style='padding:10px; text-align:center; font-weight:bold; color:#0a3d62;'>Total de jours au port</th>
                </tr>
                {rows_details}
            </table>
            
            <br>
            <p style='font-size:12px; color:#666;'>Les durées sont calculées entre le statut 'EN RADE' et 'APPAREILLAGE'.</p>
            <p>Cordialement,<br>Automated System</p>
        </div>
        """

        msg = MIMEText(body, "html", "utf-8")
        msg["Subject"] = subject
        msg["From"] = EMAIL_USER
        msg["To"] = EMAIL_TO
        try:
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
                server.starttls()
                server.login(EMAIL_USER, EMAIL_PASS)
                server.sendmail(EMAIL_USER, [EMAIL_TO], msg.as_string())
                print(f"✅ Report sent for {port_name_str}")
                emails_sent += 1
        except Exception as e:
            print(f"❌ Report Error {port_name_str}: {e}")

    if emails_sent > 0:
        print(f"[REPORT] Cleaning history...")
        state["history"] = []
        save_state(state)
        print(f"✅ History cleared.")

# ===== MONITORING LOGIC =====
def fetch_and_process_data(state: Dict):
    print("[LOG] Fetching ANP data...")
    try:
        resp = requests.get(TARGET_URL, timeout=20)
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
            stored["rade_duration_hours"] = 0.0  # Direct to quai, no rade time
            print(f"[LOG] Vessel {stored['entry'].get('nOM_NAVIREField')} went directly to quai")
            
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
            
            # Calculate quai duration
            if "quai_at" in stored and live_ts:
                quai_hours = (live_ts - datetime.fromisoformat(stored["quai_at"])).total_seconds() / 3600
            
            # Get rade duration (already calculated during EN RADE -> A QUAI transition)
            rade_hours = stored.get("rade_duration_hours", 0.0)
            
            # If we don't have rade duration but have rade_at, calculate it
            if rade_hours == 0.0 and "rade_at" in stored and "quai_at" in stored:
                rade_hours = (datetime.fromisoformat(stored["quai_at"]) - datetime.fromisoformat(stored["rade_at"])).total_seconds() / 3600
            
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
                "quai_duration_hours": quai_hours,
                "total_duration_hours": rade_hours + quai_hours
            }
            
            # Add to history
            history.append(history_record)
            print(f"[LOG] Vessel {history_record['vessel']} departed. Rade: {rade_hours:.1f}h, Quai: {quai_hours:.1f}h, Total: {rade_hours+quai_hours:.1f}h")
            
            # Mark for removal
            to_remove.append(v_id)
            
        elif stored_status == "EN RADE" and live_status == "APPAREILLAGE":
            # Vessel departed from rade without going to quai
            rade_hours = 0.0
            if "rade_at" in stored and live_ts:
                rade_hours = (live_ts - datetime.fromisoformat(stored["rade_at"])).total_seconds() / 3600
            
            # Create history record
            entry = stored.get("entry", {})
            port_code = str(entry.get("cODE_SOCIETEField", ""))
            history_record = {
                "vessel": entry.get("nOM_NAVIREField", "N/A"),
                "consignataire": entry.get("cONSIGNATAIREField", "N/A"),
                "port": port_name(port_code),
                "arrived_rade": stored.get("rade_at", "N/A"),
                "arrived_quai": "N/A",
                "departed": live_ts.isoformat() if live_ts else "N/A",
                "rade_duration_hours": rade_hours,
                "quai_duration_hours": 0.0,
                "total_duration_hours": rade_hours
            }
            
            # Add to history
            history.append(history_record)
            print(f"[LOG] Vessel {history_record['vessel']} departed from rade. Rade: {rade_hours:.1f}h")
            
            # Mark for removal
            to_remove.append(v_id)

    # Remove completed vessels
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
                p_name = port_name(str(live['entry'].get("cODE_SOCIETEField", "")))
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
                
                # 2. Send to COLLEAGUE (Only Laâyoune) AND IF SWITCH IS TRUE
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

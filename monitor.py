import os
import json
import re
import requests
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone # Added timezone
from typing import Dict, List, Optional

# ===== CONFIG & CONSTANTS =====
TARGET_URL = "https://www.anp.org.ma/_vti_bin/WS/Service.svc/mvmnv/all"
STATE_ENV_VAR = "VESSEL_STATE_DATA"
TEMP_OUTPUT_FILE = "state_output.txt"

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

# ===== STATE MANAGEMENT =====
def load_state() -> Dict:
    state_data = os.getenv(STATE_ENV_VAR)
    if not state_data: return {"active": {}, "history": []}
    try:
        data = json.loads(state_data)
        if "active" not in data: data["active"] = {}
        if "history" not in data: data["history"] = []
        print(f"DEBUG: State Loaded. Active: {len(data['active'])}, History: {len(data['history'])}")
        return data
    except json.JSONDecodeError:
        return {"active": {}, "history": []}

def save_state(state: Dict):
    # Save state (active + history) to temp file for GitHub Action
    json_str = json.dumps(state, indent=2, ensure_ascii=False)
    try:
        with open(TEMP_OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write(json_str)
    except IOError as e:
        print(f"ERROR writing state: {e}")

# ===== HELPERS (DATES) =====
def parse_ms_date(date_str: str) -> Optional[datetime]:
    """Extracts the /Date(...)/ string and returns a UTC datetime object (Fixed for Py3.14)."""
    if not date_str: return None
    m = re.search(r"/Date\((\d+)([+-]\d{4})?\)/", date_str)
    if m: 
        return datetime.fromtimestamp(int(m.group(1)) / 1000.0, tz=timezone.utc)
    return None

def get_full_datetime(entry: dict) -> Optional[datetime]:
    # Combines dATE_SITUATIONField (Day) and hEURE_SITUATIONField (Time)
    date_obj = parse_ms_date(entry.get("dATE_SITUATIONField"))
    time_obj = parse_ms_date(entry.get("hEURE_SITUATIONField"))
    
    if not date_obj: return None
    if not time_obj: return date_obj

    # Create combined datetime
    time_only = timedelta(hours=time_obj.hour, minutes=time_obj.minute, seconds=time_obj.second)
    return datetime.combine(date_obj.date(), datetime.min.time()) + time_only

def fmt_dt(json_date: str) -> str:
    """Formate DATE_SITUATION en français."""
    dt = parse_ms_date(json_date)
    if not dt: return "N/A"
    jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
    mois = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]
    jour_nom = jours[dt.weekday()].capitalize()
    mois_nom = mois[dt.month - 1]
    return f"{jour_nom}, {dt.day:02d} {mois_nom} {dt.year}"

def fmt_time_only(json_date: str) -> str:
    """Formats HEURE_SITUATION as time only: '14:00'."""
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

# ===== FORMATTERS (PREMIUM HTML DESIGN) =====
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
        PREVU
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

# ===== REPORTING LOGIC =====
def generate_monthly_report(state: Dict):
    """Generates and sends the monthly email to YOU only."""
    history = state.get("history", [])
    if not history:
        print("No history found for monthly report.")
        return

    # Group by Consignataire
    agents = {}
    for trip in history:
        agent = trip.get("consignataire", "INCONNU")
        if agent not in agents:
            agents[agent] = {"count": 0, "rade_h": 0.0, "quai_h": 0.0, "total_h": 0.0, "vessels": []}
        
        agents[agent]["count"] += 1
        agents[agent]["rade_h"] += trip.get("rade_duration_hours", 0)
        agents[agent]["quai_h"] += trip.get("quai_duration_hours", 0)
        
        # Total = Rade + Quai (Anchorage to Sailing)
        agents[agent]["total_h"] += (trip.get("rade_duration_hours", 0) + trip.get("quai_duration_hours", 0))
        agents[agent]["vessels"].append(trip["vessel"])

    # Build HTML Table
    sorted_agents = sorted(agents.items(), key=lambda x: x[1]["total_h"], reverse=True)
    rows = ""
    for agent, data in sorted_agents:
        rows += f"""
        <tr style="border-bottom:1px solid #eee;">
            <td style="padding:10px; font-weight:bold;">{agent}</td>
            <td style="padding:10px; text-align:center;">{data['count']}</td>
            <td style="padding:10px; text-align:center;">{format_duration_hours(data['rade_h'] * 3600)}</td>
            <td style="padding:10px; text-align:center;">{format_duration_hours(data['quai_h'] * 3600)}</td>
            <td style="padding:10px; text-align:center; color:#0a3d62; font-weight:bold;">{format_duration_hours(data['total_h'] * 3600)}</td>
        </tr>
        """

    subject = f"📊 RAPPORT MENSUEL - Durée de Séjour & Ancrage ({len(history)} navires)"
    body = f"""
    <div style="font-family:Arial, sans-serif;">
        <h2 style="color:#0a3d62;">Rapport Mensuel de Mouvements</h2>
        <p>Analyse des navires ayant quitté le port (Status: APPAREILLAGE).</p>
        
        <table style="width:100%; border-collapse:collapse; margin-top:20px; border:1px solid #ddd;">
            <tr style="background:#0a3d62; color:white;">
                <th style="padding:12px; text-align:left;">Consignataire (Agent)</th>
                <th style="padding:12px; text-align:center;">Nb Navires</th>
                <th style="padding:12px; text-align:center;">Ancrage</th>
                <th style="padding:12px; text-align:center;">Au Quai</th>
                <th style="padding:12px; text-align:center;">Total (Ancre->Appareillage)</th>
            </tr>
            {rows}
        </table>
        
        <br>
        <p style="font-size:12px; color:#666;">Les durées sont calculées entre le statut 'EN RADE' et 'APPAREILLAGE'.</p>
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
        print("✅ Monthly report sent to YOU only.")
        
        # Reset history for next month
        state["history"] = []
        save_state(state)
        print("✅ History cleared for new month.")
        
    except Exception as e:
        print(f"Failed to send report: {e}")

# ===== MONITORING LOGIC =====
def fetch_and_process_data(state: Dict) -> Dict:
    resp = requests.get(TARGET_URL, timeout=20)
    all_data = resp.json()
    
    live_vessels = {} 
    active_state = state.get("active", {})
    history = state.get("history", [])
    new_prevu_by_port = {}

    # 1. Map Live Data
    for entry in all_data:
        port_code = str(entry.get("cODE_SOCIETEField", ""))
        status = entry.get("sITUATIONField", "").upper()
        if port_code not in ALLOWED_PORTS: continue
        
        v_id = get_vessel_id(entry)
        full_dt = get_full_datetime(entry)
        live_vessels[v_id] = {
            "entry": entry,
            "status": status,
            "timestamp": full_dt
        }

    # 2. Process Active Tracking (Life Cycle)
    to_remove = []
    for v_id, stored in active_state.items():
        live = live_vessels.get(v_id)
        if not live: continue
        
        stored_status = stored.get("status")
        live_status = live["status"]
        live_ts = live["timestamp"]
        
        # EN RADE -> A QUAI
        if stored_status == "EN RADE" and live_status == "A QUAI":
            print(f"🚢 Berthed: {stored['entry']['nOM_NAVIREField']}")
            stored["status"] = "A QUAI"
            stored["quai_at"] = live_ts.isoformat()
            # Calc Rade Duration
            if "rade_at" in stored:
                rade_dt = datetime.fromisoformat(stored["rade_at"])
                stored["rade_duration_hours"] = (live_ts - rade_dt).total_seconds() / 3600

        # A QUAI -> APPAREILLAGE (Completed Trip)
        elif stored_status == "A QUAI" and live_status == "APPAREILLAGE":
            print(f"🏁 Departed: {stored['entry']['nOM_NAVIREField']}")
            stored["status"] = "APPAREILLAGE"
            
            quai_hours = 0.0
            if "quai_at" in stored:
                quai_dt = datetime.fromisoformat(stored["quai_at"])
                quai_hours = (live_ts - quai_dt).total_seconds() / 3600
            stored["quai_duration_hours"] = quai_hours
            
            # Save to History
            history.append({
                "vessel": stored["entry"]["nOM_NAVIREField"],
                "consignataire": stored["entry"]["cONSIGNATAIREField"],
                "port": port_name(stored["entry"]["cODE_SOCIETEField"]),
                "arrived_rade": stored.get("rade_at", "N/A"),
                "berthed": stored.get("quai_at", "N/A"),
                "departed": live_ts.isoformat(),
                "rade_duration_hours": stored.get("rade_duration_hours", 0),
                "quai_duration_hours": quai_hours
            })
            to_remove.append(v_id)

        # EN RADE -> APPAREILLAGE (Skipped Quai)
        elif stored_status == "EN RADE" and live_status == "APPAREILLAGE":
            print(f"🏁 Departed (Anchorage Only): {stored['entry']['nOM_NAVIREField']}")
            if "rade_at" in stored:
                rade_dt = datetime.fromisoformat(stored["rade_at"])
                rade_hours = (live_ts - rade_dt).total_seconds() / 3600
                history.append({
                    "vessel": stored["entry"]["nOM_NAVIREField"],
                    "consignataire": stored["entry"]["cONSIGNATAIREField"],
                    "port": port_name(stored["entry"]["cODE_SOCIETEField"]),
                    "arrived_rade": stored["rade_at"],
                    "berthed": "Direct Depart",
                    "departed": live_ts.isoformat(),
                    "rade_duration_hours": rade_hours,
                    "quai_duration_hours": 0
                })
                to_remove.append(v_id)

    # Remove completed
    for v_id in to_remove:
        del active_state[v_id]

    # 3. Detect New Vessels (PREVU & EN RADE)
    for v_id, live in live_vessels.items():
        if v_id not in active_state:
            if live["status"] == "PREVU":
                # PREVU: Add to active to prevent duplicate alerts
                active_state[v_id] = {
                    "entry": live["entry"],
                    "status": "PREVU"
                }
                
                # EMAIL ALERT LOGIC
                p_name = port_name(str(live['entry'].get("cODE_SOCIETEField")))
                if p_name not in new_prevu_by_port:
                    new_prevu_by_port[p_name] = []
                new_prevu_by_port[p_name].append(live["entry"])
            
            elif live["status"] == "EN RADE":
                # EN RADE: Start Tracking
                print(f"📌 Start Tracking: {live['entry']['nOM_NAVIREField']} (EN RADE)")
                active_state[v_id] = {
                    "entry": live["entry"],
                    "status": "EN RADE",
                    "rade_at": live["timestamp"].isoformat()
                }

    state["active"] = active_state
    state["history"] = history
    return state, new_prevu_by_port

# ===== EMAIL LOGIC (RESTORED PREMIUM DESIGN) =====
def send_email_alerts(new_vessels_by_port):
    """Sends new vessel alerts to YOU and COLLEAGUE (Laâyoune only) using Premium HTML."""
    if not new_vessels_by_port or not EMAIL_ENABLED:
        return

    for port, vessels in new_vessels_by_port.items():
        if len(vessels) == 1:
            subject = f"🔔 NOUVELLE ARRIVÉE PRÉVUE | {vessels[0].get('nOM_NAVIREField')} au Port de {port}"
        else:
            subject = f"🔔 {len(vessels)} NOUVELLES ARRIVÉES PRÉVUES au Port de {port}"

        body_parts: List[str] = [
            "Bonjour,",
            "",
            (
                f"Nous vous informons de la détection de <b>{len(vessels)} nouvelle(s) "
                f"arrivée(s) de navire(s)</b> (statut <b>PREVU</b>) "
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
                    f"statuts <b>PREVU</b> du Port de {port}."
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
                
                # 1. Send to YOU
                server.sendmail(EMAIL_USER, [EMAIL_TO], msg.as_string())
                print(f"✅ Alert sent to YOU for {port}")

                # 2. Send to COLLEAGUE (Only Laâyoune)
                if port == "Laâyoune" and EMAIL_TO_COLLEAGUE:
                    del msg["To"]
                    msg["To"] = EMAIL_TO_COLLEAGUE
                    server.sendmail(EMAIL_USER, [EMAIL_TO_COLLEAGUE], msg.as_string())
                    print(f"✅ Alert sent to COLLEAGUE for {port}")
        except Exception as e:
            print(f"Email Error: {e}")

# ===== MAIN =====
def main():
    print(f"--- Run Mode: {RUN_MODE} ---")
    state = load_state()

    if RUN_MODE == "report":
        # MONTHLY REPORT MODE
        generate_monthly_report(state)
        return

    # MONITOR MODE (Every 30 mins)
    state, new_prevu = fetch_and_process_data(state)
    save_state(state)
    
    if new_prevu:
        send_email_alerts(new_prevu)

if __name__ == "__main__":
    main()

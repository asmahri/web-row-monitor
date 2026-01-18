import os
import json
import re
import requests
import smtplib
import time
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone 
from typing import Dict, List, Optional

# ==========================================
# ⚙️ CONFIGURATION & CONSTANTS
# ==========================================
TARGET_URL = "https://www.anp.org.ma/_vti_bin/WS/Service.svc/mvmnv/all"
STATE_FILE = "state.json" 
HISTORY_FILE = "history.json"
STATE_ENV_VAR = "VESSEL_STATE_DATA" 

EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
EMAIL_TO = os.getenv("EMAIL_TO")
EMAIL_TO_COLLEAGUE = os.getenv("EMAIL_TO_COLLEAGUE") 

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
EMAIL_ENABLED = str(os.getenv("EMAIL_ENABLED", "true")).lower() == "true"
RUN_MODE = os.getenv("RUN_MODE", "monitor") 

# Target Ports: Tan Tan, Laâyoune, Dakhla
ALLOWED_PORTS = {"16", "17", "18"} 

# ==========================================
# 🌐 NETWORK RESILIENCE
# ==========================================
def fetch_vessel_data_with_retry(max_retries=3, initial_delay=5):
    """Fetch vessel data with exponential backoff retry"""
    for attempt in range(max_retries):
        try:
            print(f"[INFO] Fetching vessel data (attempt {attempt + 1}/{max_retries})")
            
            # Different timeouts for connect vs read
            resp = requests.get(
                TARGET_URL, 
                timeout=(10, 60),  # 10s connect, 60s read
                headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'application/json, text/plain, */*',
                'Accept-Language': 'fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7',
                'Accept-Encoding': 'gzip, deflate, br',
                'Referer': 'https://www.anp.org.ma/',
                'Origin': 'https://www.anp.org.ma',
                'Connection': 'keep-alive',
                'Sec-Fetch-Dest': 'empty',
                'Sec-Fetch-Mode': 'cors',
                'Sec-Fetch-Site': 'same-origin',
                'Pragma': 'no-cache',
                'Cache-Control': 'no-cache'
            }
            )
            resp.raise_for_status()
            
            # Validate response is JSON
            data = resp.json()
            if not isinstance(data, list):
                raise ValueError("API response is not a list")
                
            print(f"[SUCCESS] Fetched {len(data)} vessel records")
            return data
            
        except requests.exceptions.Timeout as e:
            print(f"[WARNING] Timeout on attempt {attempt + 1}: {e}")
            if attempt < max_retries - 1:
                wait_time = initial_delay * (2 ** attempt)  # Exponential backoff
                print(f"[INFO] Waiting {wait_time}s before retry...")
                time.sleep(wait_time)
            else:
                print("[ERROR] All retries failed due to timeout")
                raise
                
        except requests.exceptions.ConnectionError as e:
            print(f"[WARNING] Connection error on attempt {attempt + 1}: {e}")
            if attempt < max_retries - 1:
                wait_time = initial_delay * (2 ** attempt)
                print(f"[INFO] Waiting {wait_time}s before retry...")
                time.sleep(wait_time)
            else:
                print("[ERROR] All retries failed due to connection issues")
                raise
                
        except requests.exceptions.RequestException as e:
            print(f"[ERROR] Request failed: {e}")
            raise
        except ValueError as e:
            print(f"[ERROR] Invalid response format: {e}")
            raise
        except Exception as e:
            print(f"[ERROR] Unexpected error: {e}")
            raise
    
    raise Exception("All retry attempts failed")

# ==========================================
# 💾 STATE MANAGEMENT
# ==========================================
def load_state() -> Dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[WARNING] Failed to load state: {e}")

    state_data = os.getenv(STATE_ENV_VAR)
    if not state_data: return {"active": {}, "history": []}
    try:
        data = json.loads(state_data)
        return data if "active" in data else {"active": {}, "history": []}
    except (json.JSONDecodeError, TypeError) as e:
        print(f"[WARNING] Failed to parse state from env: {e}")
        return {"active": {}, "history": []}

def save_state(state: Dict):
    try:
        # Save to temp file first
        temp_file = f"{STATE_FILE}.tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        
        # Validate it's valid JSON
        with open(temp_file, "r", encoding="utf-8") as f:
            json.load(f)
        
        # Replace original
        os.replace(temp_file, STATE_FILE)
        
    except Exception as e:
        print(f"[ERROR] Save failed: {e}")
        # Clean up temp file if it exists
        if os.path.exists(temp_file):
            os.remove(temp_file)

# ==========================================
# 📅 DATE & TIME HELPERS
# ==========================================
def parse_ms_date(date_str: str) -> Optional[datetime]:
    if not date_str: return None
    m = re.search(r"/Date\((\d+)([+-]\d{4})?\)/", date_str)
    if m: 
        return datetime.fromtimestamp(int(m.group(1)) / 1000.0, tz=timezone.utc)
    return None

def fmt_dt(json_date: str) -> str:
    dt = parse_ms_date(json_date)
    if not dt: return "N/A"
    dt_m = dt.astimezone(timezone(timedelta(hours=1))) 
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
# 📧 EMAIL TEMPLATES
# ==========================================
def format_vessel_details_premium(entry: dict) -> str:
    nom = entry.get("nOM_NAVIREField") or "INCONNU"
    imo = entry.get("nUMERO_LLOYDField") or "N/A"
    cons = entry.get("cONSIGNATAIREField") or "N/A"
    escale = entry.get("nUMERO_ESCALEField") or "N/A"
    eta_line = f"{fmt_dt(entry.get('dATE_SITUATIONField'))} {fmt_time_only(entry.get('hEURE_SITUATIONField'))}"
    prov = entry.get("pROVField") or "Inconnue"
    type_nav = entry.get("tYP_NAVIREField") or "N/A"

    return f"""
    <div style="font-family: Arial, sans-serif; margin: 15px 0; border: 1px solid #d0d7e1; border-radius: 8px; overflow: hidden;">
        <div style="background: #0a3d62; color: white; padding: 12px; font-size: 16px;">
            🚢 <b>{nom}</b>
        </div>
        <table style="width: 100%; border-collapse: collapse; font-size: 14px;">
            <tr><td style="padding: 10px; border-bottom: 1px solid #eeeeee; width: 30%;"><b>🕒 ETA</b></td><td style="padding: 10px; border-bottom: 1px solid #eeeeee;">{eta_line}</td></tr>
            <tr><td style="padding: 10px; border-bottom: 1px solid #eeeeee;"><b>🆔 IMO</b></td><td style="padding: 10px; border-bottom: 1px solid #eeeeee;">{imo}</td></tr>
            <tr><td style="padding: 10px; border-bottom: 1px solid #eeeeee;"><b>⚓ Escale</b></td><td style="padding: 10px; border-bottom: 1px solid #eeeeee;">{escale}</td></tr>
            <tr><td style="padding: 10px; border-bottom: 1px solid #eeeeee;"><b>🛳️ Type</b></td><td style="padding: 10px; border-bottom: 1px solid #eeeeee;">{type_nav}</td></tr>
            <tr><td style="padding: 10px; border-bottom: 1px solid #eeeeee;"><b>🏢 Agent</b></td><td style="padding: 10px; border-bottom: 1px solid #eeeeee;">{cons}</td></tr>
            <tr><td style="padding: 10px;"><b>🌍 Prov.</b></td><td style="padding: 10px;">{prov}</td></tr>
        </table>
    </div>"""

def send_monthly_report(history: list, specific_port: str):
    if not history: return

    # 1. Process Stats
    stats = {}
    for h in history:
        agent = h.get('agent', 'Inconnu')
        if agent not in stats: stats[agent] = {"calls": 0, "quay_sum": 0.0, "anch_sum": 0.0}
        stats[agent]["calls"] += 1
        stats[agent]["quay_sum"] += h.get('duration', 0)
        stats[agent]["anch_sum"] += h.get('anchorage_duration', 0)

    # 2. Build Agent Statistics Table
    agent_rows = ""
    sorted_agents = sorted(stats.items(), key=lambda x: x[1]['calls'], reverse=True)
    for agent, data in sorted_agents:
        total_calls = data['calls']
        agent_rows += f"""
        <tr style="border-bottom: 1px solid #e0e0e0;">
            <td style="padding: 10px; font-weight: bold; color: #333;">{agent}</td>
            <td style="padding: 10px; text-align: center;">{total_calls}</td>
            <td style="padding: 10px; text-align: center;">{round(data['anch_sum']/total_calls, 1)}h</td>
            <td style="padding: 10px; text-align: center;">{round(data['quay_sum']/total_calls, 1)}h</td>
        </tr>"""

    # 3. Build Detailed Vessel History Table
    vessel_rows = ""
    sorted_history = sorted(history, key=lambda x: x.get('departure', ''), reverse=True)
    for h in sorted_history:
        try:
            dt_obj = datetime.fromisoformat(h['departure'])
            date_str = dt_obj.astimezone(timezone(timedelta(hours=1))).strftime("%d/%m/%Y %H:%M")
        except: date_str = "N/A"
        
        vessel_rows += f"""
        <tr style="border-bottom: 1px solid #f0f0f0;">
            <td style="padding: 8px; color: #333;">{h['vessel']}</td>
            <td style="padding: 8px; font-size: 12px;">{h.get('agent', '-')}</td>
            <td style="padding: 8px; text-align: center;">{h.get('anchorage_duration', 0)}h</td>
            <td style="padding: 8px; text-align: center;">{h.get('duration', 0)}h</td>
            <td style="padding: 8px; font-size: 12px;">{date_str}</td>
        </tr>"""

    subject = f"📊 Rapport Mensuel : Port de {specific_port} ({len(history)} Mouvements)"
    body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 900px; margin: auto;">
        <div style="background: #0a3d62; color: white; padding: 15px; border-radius: 8px 8px 0 0;">
            <h2 style="margin: 0; font-size: 20px;">📊 Rapport de Performance</h2>
            <p style="margin: 5px 0 0; opacity: 0.9; font-size: 14px;">Port de {specific_port} - Statistiques Mensuelles</p>
        </div>
        <div style="background: #f8f9fa; padding: 20px; border: 1px solid #d0d7e1; border-top: none; border-radius: 0 0 8px 8px;">
            <p>Bonjour,</p>
            <p>Voici le récapitulatif d'activité mensuel pour le <b>Port de {specific_port}</b>.</p>
            
            <h3 style="color: #0a3d62; border-bottom: 2px solid #0a3d62; padding-bottom: 10px;">🏢 Statistiques par Agent</h3>
            <table style="width: 100%; border-collapse: collapse; background: white; margin-bottom: 30px; border-radius: 4px; overflow: hidden;">
                <thead><tr style="background: #e9ecef; text-align: left;">
                    <th style="padding: 12px;">Agent</th><th style="padding: 12px; text-align: center;">Escales</th>
                    <th style="padding: 12px; text-align: center;">⚓ Attente</th><th style="padding: 12px; text-align: center;">🏗️ Quai</th>
                </tr></thead>
                <tbody>{agent_rows}</tbody>
            </table>

            <h3 style="color: #0a3d62; border-bottom: 2px solid #0a3d62; padding-bottom: 10px;">📋 Liste Détaillée</h3>
            <table style="width: 100%; border-collapse: collapse; background: white; font-size: 13px; border-radius: 4px; overflow: hidden;">
                <thead><tr style="background: #e9ecef; text-align: left;">
                    <th style="padding: 10px;">Navire</th><th style="padding: 10px;">Agent</th>
                    <th style="padding: 10px; text-align: center;">⚓ Poste</th><th style="padding: 10px; text-align: center;">🏗️ Quai</th>
                    <th style="padding: 10px;">Date</th>
                </tr></thead>
                <tbody>{vessel_rows}</tbody>
            </table>
            
            <div style='margin-top: 30px; border-top: 1px solid #e6e9ef; padding-top: 15px;'>
                <p style='font-size:14px; color:#333;'>Cordialement,</p>
                <p style='font-size:12px; color:#777777; font-style: italic;'>Ceci est une génération automatique par le système de surveillance.</p>
            </div>
        </div>
    </div>"""
    send_email(EMAIL_TO, subject, body)

def send_email(to, sub, body):
    if not EMAIL_ENABLED or not EMAIL_USER: return
    msg = MIMEText(body, "html", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = sub, EMAIL_USER, to
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as server:  # Increased email timeout too
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

    # REPORT MODE Logic
    if RUN_MODE == "report":
        print(f"[LOG] Generating monthly reports for {len(history)} movements.")
        for p_code in ALLOWED_PORTS:
            p_name = port_name(p_code)
            p_hist = [h for h in history if h.get("port") == p_name]
            if p_hist:
                print(f"[LOG] Sending report for {p_name}")
                send_monthly_report(p_hist, p_name)
        
        # Move to history.json and clear state history
        archive_file = "history.json"
        existing_archive = []
        if os.path.exists(archive_file):
            try:
                with open(archive_file, "r", encoding="utf-8") as f:
                    existing_archive = json.load(f)
            except Exception as e:
                print(f"[WARNING] Failed to load history archive: {e}")
        
        existing_archive.extend(history)
        
        try:
            with open(archive_file, "w", encoding="utf-8") as f:
                json.dump(existing_archive, f, indent=2, ensure_ascii=False)
            print(f"[LOG] Moved {len(history)} completed movements to history.json")
        except Exception as e:
            print(f"[ERROR] Failed to save history archive: {e}")
            return
        
        state["history"] = []
        save_state(state)
        print("[LOG] Monthly report completed. State history cleared.")
        return

    # MONITOR MODE Logic with retry
    try:
        all_data = fetch_vessel_data_with_retry(max_retries=3, initial_delay=5)
    except Exception as e:
        print(f"[CRITICAL] API Error after retries: {e}")
        return

    now_utc = datetime.now(timezone.utc)
    live_vessels = {}
    for e in all_data:
        if str(e.get("cODE_SOCIETEField")) in ALLOWED_PORTS:
            v_id = f"{e.get('nUMERO_LLOYDField','0')}-{e.get('nUMERO_ESCALEField','0')}"
            live_vessels[v_id] = {"e": e, "status": (e.get("sITUATIONField") or "").upper()}

    alerts, to_remove = {}, []

    # Transitions & Tracking
    for v_id, stored in active.items():
        live = live_vessels.get(v_id)
        if live:
            prev, new = stored["status"], live["status"]
            
            if new == "EN RADE" and prev != "EN RADE":
                stored["anchored_at"] = now_utc.isoformat()
                
            if prev != "A QUAI" and new == "A QUAI":
                stored["quai_at"] = now_utc.isoformat()
                anch_start = stored.get("anchored_at", now_utc.isoformat())
                stored["anchorage_duration"] = round(calculate_duration_hours(anch_start, now_utc), 2)
                
            if prev == "A QUAI" and new == "APPAREILLAGE":
                q_start = stored.get("quai_at", stored["last_seen"])
                history.append({
                    "vessel": stored["entry"].get('nOM_NAVIREField'),
                    "agent": stored["entry"].get("cONSIGNATAIREField", "Inconnu"),
                    "port": port_name(stored["entry"].get('cODE_SOCIETEField')),
                    "duration": round(calculate_duration_hours(q_start, now_utc), 2),
                    "anchorage_duration": stored.get("anchorage_duration", 0.0),
                    "departure": now_utc.isoformat()
                })
                to_remove.append(v_id)
                
            stored.update({"status": new, "last_seen": now_utc.isoformat()})

    for vid in to_remove: active.pop(vid, None)

    # New Vessels (PREVU Alerts)
    for v_id, live in live_vessels.items():
        if v_id not in active:
            # First run check: ignore non-PREVU to avoid false alerts on existing ships
            if len(active) == 0 and live["status"] != "PREVU": continue
            
            active[v_id] = {"entry": live["e"], "status": live["status"], "last_seen": now_utc.isoformat()}
            if live["status"] == "PREVU":
                p = port_name(live['e'].get("cODE_SOCIETEField"))
                alerts.setdefault(p, []).append(live["e"])

    # State Cleanup & Save
    cutoff = now_utc - timedelta(days=3)
    state["active"] = {k: v for k, v in active.items() if datetime.fromisoformat(v["last_seen"]).replace(tzinfo=timezone.utc) > cutoff}
    state["history"] = history[-1000:]
    save_state(state)

    # Send Arrival Alerts
    if alerts:
        for p, vessels in alerts.items():
            v_names = ", ".join([v.get('nOM_NAVIREField', 'Unknown') for v in vessels])
            intro = f"<p style='font-family:Arial; font-size:15px;'>Bonjour,<br><br>Mouvements prévus au <b>Port de {p}</b> :</p>"
            cards = "".join([format_vessel_details_premium(v) for v in vessels])
            footer = "<p style='font-size:12px; color:#777; font-style:italic;'>Rapport automatique par le système de surveillance.</p>"
            
            full_body = intro + cards + footer
            subject = f"🔔 NOUVELLE ARRIVÉE PRÉVUE | {v_names} au Port de {p}"
            
            send_email(EMAIL_TO, subject, full_body)
            if p == "Laâyoune" and EMAIL_TO_COLLEAGUE:
                send_email(EMAIL_TO_COLLEAGUE, subject, full_body)
    else:
        print("[LOG] No new PREVU vessels.")

if __name__ == "__main__":
    main()

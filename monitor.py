import os
import json
import re
import requests
import smtplib
import time
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone 
from typing import Dict, List, Optional
from collections import defaultdict

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

# Status categories for time tracking
ANCHORAGE_STATUSES = {"EN RADE", "EN ATTENTE"}
BERTH_STATUSES = {"A QUAI", "EN DECHARGEMENT", "EN CHARGEMENT"}
COMPLETED_STATUSES = {"APPAREILLAGE", "TERMINE"}

# ==========================================
# 🌐 NETWORK RESILIENCE
# ==========================================
def fetch_vessel_data_with_retry(max_retries=3, initial_delay=5):
    """Fetch vessel data with exponential backoff retry"""
    for attempt in range(max_retries):
        try:
            print(f"[INFO] Fetching vessel data (attempt {attempt + 1}/{max_retries})")
            
            # Browser-like headers that fixed the 400 error
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
            
            resp = requests.get(
                TARGET_URL, 
                timeout=(10, 60),
                headers=headers,
                verify=True
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
                wait_time = initial_delay * (2 ** attempt)
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
                
        except requests.exceptions.HTTPError as e:
            print(f"[ERROR] HTTP Error {resp.status_code}: {e}")
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
# 📊 ANALYTICS ENGINE
# ==========================================
def update_vessel_timers(active_vessel: Dict, new_status: str, now_utc: datetime) -> Dict:
    """
    Update time accumulators based on status changes
    Returns updated vessel data
    """
    current_status = active_vessel.get("current_status", "UNKNOWN")
    last_updated_str = active_vessel.get("last_updated")
    
    # Only track time if we have a previous timestamp
    if last_updated_str:
        try:
            last_updated = datetime.fromisoformat(last_updated_str)
            # Calculate elapsed time since last update (in hours)
            elapsed_hours = (now_utc - last_updated).total_seconds() / 3600.0
            
            # Add elapsed time to appropriate accumulator
            if current_status in ANCHORAGE_STATUSES:
                active_vessel["anchorage_hours"] = active_vessel.get("anchorage_hours", 0.0) + elapsed_hours
            elif current_status in BERTH_STATUSES:
                active_vessel["berth_hours"] = active_vessel.get("berth_hours", 0.0) + elapsed_hours
        except:
            pass  # If timestamp parsing fails, skip time tracking
    
    # Update status and timestamp
    active_vessel["current_status"] = new_status
    active_vessel["last_updated"] = now_utc.isoformat()
    active_vessel["last_seen"] = now_utc.isoformat()
    
    return active_vessel

def calculate_performance_note(avg_anchorage: float, avg_berth: float) -> str:
    """Generate human-readable performance note"""
    if avg_anchorage < 5 and avg_berth < 24:
        return "⭐ Excellent - Fast operations"
    elif avg_anchorage < 10 and avg_berth < 36:
        return "✅ Good - Efficient"
    elif avg_anchorage < 24:
        return "⚠️ Moderate - Some waiting time"
    else:
        return "🐌 Slow - Long waiting periods"

# ==========================================
# 📧 EMAIL TEMPLATES (BI UPGRADE)
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
    if not history: 
        print(f"[INFO] No history data for {specific_port}")
        return

    # 1. Group history by agent for performance analytics
    agent_stats = defaultdict(lambda: {"calls": 0, "total_anchorage": 0.0, "total_berth": 0.0})
    
    for h in history:
        agent = h.get('agent', 'Inconnu')
        agent_stats[agent]["calls"] += 1
        agent_stats[agent]["total_anchorage"] += h.get('anchorage_hours', 0)
        agent_stats[agent]["total_berth"] += h.get('berth_hours', 0)

    # 2. Build Agent Performance Summary Table
    agent_rows = ""
    sorted_agents = sorted(agent_stats.items(), key=lambda x: x[1]['calls'], reverse=True)
    
    for agent, data in sorted_agents:
        avg_anchorage = round(data['total_anchorage'] / data['calls'], 1) if data['calls'] > 0 else 0
        avg_berth = round(data['total_berth'] / data['calls'], 1) if data['calls'] > 0 else 0
        performance_note = calculate_performance_note(avg_anchorage, avg_berth)
        
        agent_rows += f"""
        <tr style="border-bottom: 1px solid #e0e0e0;">
            <td style="padding: 10px; font-weight: bold; color: #2c3e50;">{agent}</td>
            <td style="padding: 10px; text-align: center; font-weight: bold;">{data['calls']}</td>
            <td style="padding: 10px; text-align: center; color: {'#e74c3c' if avg_anchorage > 12 else '#27ae60'}">{avg_anchorage} Hrs</td>
            <td style="padding: 10px; text-align: center; color: {'#f39c12' if avg_berth > 36 else '#27ae60'}">{avg_berth} Hrs</td>
            <td style="padding: 10px; text-align: center; font-size: 12px;">{performance_note}</td>
        </tr>"""

    # 3. Build Detailed Vessel Statistics Table
    vessel_rows = ""
    sorted_history = sorted(history, key=lambda x: x.get('departure', ''), reverse=True)
    
    for h in sorted_history:
        try:
            dt_obj = datetime.fromisoformat(h['departure'])
            date_str = dt_obj.astimezone(timezone(timedelta(hours=1))).strftime("%d/%m/%Y %H:%M")
        except: 
            date_str = "N/A"
        
        total_hours = h.get('anchorage_hours', 0) + h.get('berth_hours', 0)
        
        vessel_rows += f"""
        <tr style="border-bottom: 1px solid #f0f0f0;">
            <td style="padding: 8px; color: #2c3e50; font-weight: bold;">{h['vessel']}</td>
            <td style="padding: 8px; font-size: 13px;">{h.get('agent', '-')}</td>
            <td style="padding: 8px; text-align: center; color: {'#e74c3c' if h.get('anchorage_hours', 0) > 12 else '#27ae60'}">{round(h.get('anchorage_hours', 0), 1)} Hrs</td>
            <td style="padding: 8px; text-align: center; color: {'#f39c12' if h.get('berth_hours', 0) > 36 else '#27ae60'}">{round(h.get('berth_hours', 0), 1)} Hrs</td>
            <td style="padding: 8px; text-align: center; font-weight: bold;">{round(total_hours, 1)} Hrs</td>
            <td style="padding: 8px; font-size: 12px;">{date_str}</td>
        </tr>"""

    subject = f"📊 Rapport Mensuel BI : Port de {specific_port} ({len(history)} Escales)"
    
    body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 1100px; margin: auto;">
        <div style="background: linear-gradient(135deg, #0a3d62 0%, #1e5799 100%); color: white; padding: 20px; border-radius: 10px 10px 0 0;">
            <h2 style="margin: 0; font-size: 24px;">📊 Business Intelligence Report - {specific_port}</h2>
            <p style="margin: 10px 0 0; opacity: 0.95; font-size: 16px;">Analyse des Performances Mensuelles</p>
            <p style="margin: 5px 0 0; opacity: 0.85; font-size: 14px;">{len(history)} escales complétées | Données au {datetime.now().strftime('%d/%m/%Y')}</p>
        </div>
        
        <div style="background: #f8f9fa; padding: 25px; border: 1px solid #d0d7e1; border-top: none; border-radius: 0 0 10px 10px;">
            <p>Bonjour,</p>
            <p>Voici le rapport d'analyse d'activité mensuel pour le <b>Port de {specific_port}</b> avec les nouvelles métriques Business Intelligence.</p>
            
            <div style="margin: 30px 0; padding: 15px; background: #e8f4fc; border-radius: 8px; border-left: 4px solid #3498db;">
                <h3 style="margin: 0 0 10px 0; color: #2980b9;">📈 KPIs Clés du Port</h3>
                <p style="margin: 5px 0; font-size: 14px;">
                    <strong>Escales totales:</strong> {len(history)} |
                    <strong>Temps d'attente moyen:</strong> {round(sum(h.get('anchorage_hours', 0) for h in history)/len(history), 1)}h |
                    <strong>Temps à quai moyen:</strong> {round(sum(h.get('berth_hours', 0) for h in history)/len(history), 1)}h |
                    <strong>Durée totale moyenne:</strong> {round(sum(h.get('anchorage_hours', 0) + h.get('berth_hours', 0) for h in history)/len(history), 1)}h
                </p>
            </div>

            <h3 style="color: #0a3d62; border-bottom: 3px solid #0a3d62; padding-bottom: 12px; margin-top: 30px;">
                🏢 Tableau 1 : Performance des Agents Maritimes
            </h3>
            <table style="width: 100%; border-collapse: collapse; background: white; margin-bottom: 40px; border-radius: 6px; overflow: hidden; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
                <thead>
                    <tr style="background: linear-gradient(135deg, #2c3e50 0%, #4a6491 100%); color: white; text-align: left;">
                        <th style="padding: 15px; font-weight: bold;">Agent Maritime</th>
                        <th style="padding: 15px; text-align: center; font-weight: bold;">Escales</th>
                        <th style="padding: 15px; text-align: center; font-weight: bold;">⏳ Attente Moy.</th>
                        <th style="padding: 15px; text-align: center; font-weight: bold;">🏗️ Quai Moy.</th>
                        <th style="padding: 15px; text-align: center; font-weight: bold;">📝 Performance</th>
                    </tr>
                </thead>
                <tbody>{agent_rows}</tbody>
            </table>

            <h3 style="color: #0a3d62; border-bottom: 3px solid #0a3d62; padding-bottom: 12px;">
                📋 Tableau 2 : Statistiques Détailées par Navire
            </h3>
            <table style="width: 100%; border-collapse: collapse; background: white; font-size: 13px; border-radius: 6px; overflow: hidden; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
                <thead>
                    <tr style="background: #ecf0f1; text-align: left; color: #2c3e50;">
                        <th style="padding: 12px; border-bottom: 2px solid #bdc3c7;">Navire</th>
                        <th style="padding: 12px; border-bottom: 2px solid #bdc3c7;">Agent</th>
                        <th style="padding: 12px; border-bottom: 2px solid #bdc3c7; text-align: center;">⏳ Attente</th>
                        <th style="padding: 12px; border-bottom: 2px solid #bdc3c7; text-align: center;">🏗️ Quai</th>
                        <th style="padding: 12px; border-bottom: 2px solid #bdc3c7; text-align: center;">⌛ Total</th>
                        <th style="padding: 12px; border-bottom: 2px solid #bdc3c7;">Date Départ</th>
                    </tr>
                </thead>
                <tbody>{vessel_rows}</tbody>
            </table>
            
            <div style='margin-top: 40px; padding-top: 20px; border-top: 2px solid #e6e9ef;'>
                <h4 style='color: #2c3e50; margin-bottom: 15px;'>🔍 Insights Clés pour {specific_port}</h4>
                <ul style='font-size: 14px; color: #34495e; line-height: 1.6;'>
                    <li><strong>Négociation:</strong> Utilisez ces données pour les négociations de tarifs portuaires avec les agents</li>
                    <li><strong>Planification:</strong> Prévoyez les ressources en fonction des temps d'attente moyens</li>
                    <li><strong>Benchmarking:</strong> Comparez les performances entre agents pour identifier les meilleures pratiques</li>
                    <li><strong>Optimisation:</strong> Ciblez les agents avec les plus longs temps d'attente pour amélioration</li>
                </ul>
                
                <div style='background: #f9f9f9; padding: 15px; border-radius: 6px; margin-top: 20px;'>
                    <p style='font-size:14px; color:#333; margin: 0;'><strong>Cordialement,</strong></p>
                    <p style='font-size:12px; color:#777777; font-style: italic; margin: 5px 0 0 0;'>
                        Rapport BI généré automatiquement par le système de surveillance ANP.<br>
                        Surveillance en temps réel | Métriques calculées toutes les 30 minutes
                    </p>
                </div>
            </div>
        </div>
    </div>"""
    
    send_email(EMAIL_TO, subject, body)
    
    # Send duplicate to colleague for Laâyoune only
    if specific_port == "Laâyoune" and EMAIL_TO_COLLEAGUE:
        send_email(EMAIL_TO_COLLEAGUE, subject, body)

def send_email(to, sub, body):
    if not EMAIL_ENABLED or not EMAIL_USER: 
        print(f"[INFO] Email sending disabled or no email user configured")
        return
    
    msg = MIMEText(body, "html", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = sub, EMAIL_USER, to
    
    try:
        print(f"[INFO] Sending email to {to}")
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASS)
            server.sendmail(EMAIL_USER, [to], msg.as_string())
        print(f"[SUCCESS] Email sent successfully to {to}")
    except smtplib.SMTPAuthenticationError as e:
        print(f"[ERROR] Email authentication failed: {e}")
    except smtplib.SMTPException as e:
        print(f"[ERROR] SMTP error: {e}")
    except Exception as e:
        print(f"[ERROR] Email sending failed: {e}")

# ==========================================
# 🔄 MAIN PROCESS (BI UPGRADE)
# ==========================================
def main():
    print(f"{'='*50}\n🚢 VESSEL MONITOR - Business Intelligence Edition\n{'='*50}")
    print(f"MODE: {RUN_MODE.upper()}\nPorts: Laâyoune (17), Tan Tan (16), Dakhla (18)")
    print(f"{'='*50}")
    
    state = load_state()
    active = state.get("active", {})
    history = state.get("history", [])

    # REPORT MODE Logic
    if RUN_MODE == "report":
        print(f"[BI] Generating monthly BI reports for {len(history)} movements.")
        for p_code in ALLOWED_PORTS:
            p_name = port_name(p_code)
            p_hist = [h for h in history if h.get("port") == p_name]
            if p_hist:
                print(f"[BI] Sending BI report for {p_name} ({len(p_hist)} escales)")
                send_monthly_report(p_hist, p_name)
            else:
                print(f"[BI] No data for {p_name}")
        
        # Archive to history.json and clear state history
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
            print(f"[LOG] Archived {len(history)} movements to history.json")
        except Exception as e:
            print(f"[ERROR] Failed to save history archive: {e}")
            return
        
        state["history"] = []
        save_state(state)
        print("[LOG] Monthly BI reports completed. State history cleared.")
        return

    # MONITOR MODE Logic with BI tracking
    try:
        all_data = fetch_vessel_data_with_retry(max_retries=3, initial_delay=5)
    except Exception as e:
        print(f"[CRITICAL] API Error after retries: {e}")
        return

    now_utc = datetime.now(timezone.utc)
    live_vessels = {}
    
    # Process live data
    for e in all_data:
        if str(e.get("cODE_SOCIETEField")) in ALLOWED_PORTS:
            v_id = f"{e.get('nUMERO_LLOYDField','0')}-{e.get('nUMERO_ESCALEField','0')}"
            live_vessels[v_id] = {
                "e": e, 
                "status": (e.get("sITUATIONField") or "").upper()
            }

    alerts, to_remove = {}, []
    
    # Update active vessels with BI tracking
    for v_id, stored in active.items():
        live = live_vessels.get(v_id)
        
        if live:
            new_status = live["status"]
            prev_status = stored.get("current_status", stored.get("status", "UNKNOWN"))
            
            # Update time accumulators based on status change
            stored = update_vessel_timers(stored, new_status, now_utc)
            
            # Check for state transitions
            if prev_status == "A QUAI" and new_status == "APPAREILLAGE":
                # Vessel completed its stay - add to history
                history.append({
                    "vessel": stored["entry"].get('nOM_NAVIREField', 'Unknown'),
                    "agent": stored["entry"].get("cONSIGNATAIREField", "Inconnu"),
                    "port": port_name(stored["entry"].get('cODE_SOCIETEField')),
                    "anchorage_hours": round(stored.get("anchorage_hours", 0.0), 1),
                    "berth_hours": round(stored.get("berth_hours", 0.0), 1),
                    "departure": now_utc.isoformat()
                })
                to_remove.append(v_id)
                print(f"[LOG] Vessel {stored['entry'].get('nOM_NAVIREField')} completed stay. Anchorage: {stored.get('anchorage_hours', 0):.1f}h, Berth: {stored.get('berth_hours', 0):.1f}h")
            
            # Update entry data
            stored["entry"] = live["e"]
            
        else:
            # Vessel not in live data - keep tracking time if in a tracked status
            current_status = stored.get("current_status", "UNKNOWN")
            if current_status in ANCHORAGE_STATUSES.union(BERTH_STATUSES):
                stored = update_vessel_timers(stored, current_status, now_utc)
    
    # Remove completed vessels
    for vid in to_remove: 
        active.pop(vid, None)

    # New Vessels (PREVU Alerts)
    for v_id, live in live_vessels.items():
        if v_id not in active:
            # First run check: ignore non-PREVU to avoid false alerts on existing ships
            if len(active) == 0 and live["status"] != "PREVU": 
                continue
            
            # Initialize new vessel with BI tracking structure
            active[v_id] = {
                "entry": live["e"],
                "current_status": live["status"],
                "anchorage_hours": 0.0,
                "berth_hours": 0.0,
                "last_updated": now_utc.isoformat(),
                "last_seen": now_utc.isoformat()
            }
            
            if live["status"] == "PREVU":
                p = port_name(live['e'].get("cODE_SOCIETEField"))
                alerts.setdefault(p, []).append(live["e"])
                print(f"[ALERT] New PREVU vessel: {live['e'].get('nOM_NAVIREField')} at {p}")

    # State Cleanup (3 days for vanished vessels)
    cutoff = now_utc - timedelta(days=3)
    active_cleaned = {}
    for k, v in active.items():
        last_seen_str = v.get("last_seen", now_utc.isoformat())
        try:
            last_seen = datetime.fromisoformat(last_seen_str)
            if last_seen.replace(tzinfo=timezone.utc) > cutoff:
                active_cleaned[k] = v
            else:
                print(f"[CLEANUP] Removed vanished vessel: {v['entry'].get('nOM_NAVIREField')}")
        except:
            active_cleaned[k] = v  # Keep if we can't parse timestamp
    
    state["active"] = active_cleaned
    state["history"] = history[-1000:]  # Keep last 1000 history entries
    save_state(state)
    
    # Print current tracking stats
    print(f"[STATS] Tracking {len(active_cleaned)} active vessels")
    print(f"[STATS] Total history entries: {len(history)}")

    # Send Arrival Alerts
    if alerts:
        for p, vessels in alerts.items():
            v_names = ", ".join([v.get('nOM_NAVIREField', 'Unknown') for v in vessels])
            intro = f"<p style='font-family:Arial; font-size:15px;'>Bonjour,<br><br>Mouvements prévus au <b>Port de {p}</b> :</p>"
            cards = "".join([format_vessel_details_premium(v) for v in vessels])
            footer = "<p style='font-size:12px; color:#777; font-style:italic;'>Rapport automatique par le système de surveillance BI.</p>"
            
            full_body = intro + cards + footer
            subject = f"🔔 NOUVELLE ARRIVÉE PRÉVUE | {v_names} au Port de {p}"
            
            send_email(EMAIL_TO, subject, full_body)
            if p == "Laâyoune" and EMAIL_TO_COLLEAGUE:
                send_email(EMAIL_TO_COLLEAGUE, subject, full_body)
                
        print(f"[ALERT] Sent {sum(len(v) for v in alerts.values())} arrival alerts")
    else:
        print("[LOG] No new PREVU vessels.")

if __name__ == "__main__":
    main()

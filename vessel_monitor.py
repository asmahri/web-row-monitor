import os
import json
import re
import shutil
import requests
import smtplib
import time
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional
from collections import defaultdict

# ==========================================
# ⚙️ CONFIGURATION & CONSTANTS
# ==========================================
TARGET_URL    = "https://www.anp.org.ma/_vti_bin/WS/Service.svc/mvmnv/all"
STATE_FILE    = "state.json"
HISTORY_FILE  = "history.json"
STATE_ENV_VAR = "VESSEL_STATE_DATA"

EMAIL_USER         = os.getenv("EMAIL_USER")
EMAIL_PASS         = os.getenv("EMAIL_PASS")
EMAIL_TO           = os.getenv("EMAIL_TO")
EMAIL_TO_COLLEAGUE = os.getenv("EMAIL_TO_COLLEAGUE")

SMTP_SERVER   = "smtp.gmail.com"
SMTP_PORT     = 587
EMAIL_ENABLED = str(os.getenv("EMAIL_ENABLED", "true")).lower() == "true"
RUN_MODE      = os.getenv("RUN_MODE", "monitor")

# Target Ports: Safi (03), Nador (06), Jorf Lasfar (07)
ALLOWED_PORTS = {"03", "06", "07"}

# Status categories for tracking
ANCHORAGE_STATUSES = {"EN RADE"}
BERTH_STATUSES     = {"A QUAI"}
COMPLETED_STATUSES = {"APPAREILLAGE", "TERMINE"}
PLANNED_STATUSES   = {"PREVU"}

# ==========================================
# ⚙️ STARTUP VALIDATION
# ==========================================
def validate_config():
    """Warn early if critical env vars are missing or RUN_MODE is invalid."""
    missing = [v for v in ("EMAIL_USER", "EMAIL_PASS", "EMAIL_TO") if not os.getenv(v)]
    if missing:
        print(f"[WARNING] Missing env vars: {', '.join(missing)} — emails will be disabled.")
    valid_modes = {"monitor", "report"}
    if RUN_MODE not in valid_modes:
        raise ValueError(f"[FATAL] RUN_MODE='{RUN_MODE}' is invalid. Must be one of: {valid_modes}")

# ==========================================
# 🚦 STATUS CLEANING
# ==========================================
def clean_status(raw_status: str) -> str:
    """Sanitize and validate status from API."""
    if not raw_status:
        return "UNKNOWN"
    status = raw_status.strip().upper()
    expected_statuses = {"PREVU", "EN RADE", "A QUAI", "APPAREILLAGE", "TERMINE"}
    if status not in expected_statuses:
        print(f"[WARNING] Unexpected API Status: '{raw_status}'")
    return status

# ==========================================
# 🌐 NETWORK RESILIENCE
# ==========================================
def fetch_vessel_data_with_retry(max_retries=3, initial_delay=5):
    """Fetch vessel data with full browser spoofing to bypass WAFs."""
    for attempt in range(max_retries):
        try:
            print(f"[INFO] Fetching vessel data (attempt {attempt + 1}/{max_retries})")
            headers = {
                'User-Agent':      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept':          'application/json, text/plain, */*',
                'Accept-Language': 'fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7',
                'Accept-Encoding': 'gzip, deflate, br',
                'Referer':         'https://www.anp.org.ma/',
                'Origin':          'https://www.anp.org.ma',
                'Connection':      'keep-alive',
                'Sec-Fetch-Dest':  'empty',
                'Sec-Fetch-Mode':  'cors',
                'Sec-Fetch-Site':  'same-origin',
                'Pragma':          'no-cache',
                'Cache-Control':   'no-cache',
            }
            resp = requests.get(TARGET_URL, timeout=(10, 60), headers=headers)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                raise ValueError("API response is not a list")
            print(f"[SUCCESS] Fetched {len(data)} vessel records")
            return data
        except (requests.exceptions.RequestException, ValueError) as e:
            print(f"[WARNING] Attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                time.sleep(initial_delay * (2 ** attempt))
            else:
                raise
    raise Exception("All retry attempts failed")

# ==========================================
# 💾 STATE MANAGEMENT
# ==========================================
def load_state() -> Dict:
    """Load state with multi-source validation."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and "active" in data and "history" in data:
                    return data
        except Exception as e:
            print(f"[WARNING] Local state load failed: {e}")

    state_data = os.getenv(STATE_ENV_VAR)
    if state_data:
        try:
            data = json.loads(state_data)
            if isinstance(data, dict) and "active" in data and "history" in data:
                return data
        except Exception:
            pass

    return {"active": {}, "history": []}

def save_state(state: Dict):
    """Save state with transactional backup logic."""
    try:
        if os.path.exists(STATE_FILE):
            shutil.copy2(STATE_FILE, f"{STATE_FILE}.backup")
        temp_file = f"{STATE_FILE}.tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(temp_file, STATE_FILE)
    except Exception as e:
        print(f"[CRITICAL] State save failed: {e}")

# ==========================================
# 📅 DATE & TIME HELPERS
# ==========================================
def parse_ms_date(date_str: str) -> Optional[datetime]:
    if not date_str:
        return None
    m = re.search(r"/Date\((\d+)([+-]\d{4})?\)/", date_str)
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000.0, tz=timezone.utc)
    return None

def fmt_dt(json_date: str) -> str:
    dt = parse_ms_date(json_date)
    if not dt:
        return "N/A"
    dt_m = dt.astimezone(timezone(timedelta(hours=1)))
    jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
    mois  = ["janvier", "février", "mars", "avril", "mai", "juin",
             "juillet", "août", "septembre", "octobre", "novembre", "décembre"]
    return f"{jours[dt_m.weekday()].capitalize()}, {dt_m.day:02d} {mois[dt_m.month - 1]} {dt_m.year}"

def fmt_time_only(json_date: str) -> str:
    dt = parse_ms_date(json_date)
    if not dt:
        return "N/A"
    return dt.astimezone(timezone(timedelta(hours=1))).strftime("%H:%M")

def port_name(code: str) -> str:
    return {"03": "Safi", "06": "Nador", "07": "Jorf Lasfar"}.get(str(code), f"Port {code}")

def _ensure_aware(dt: datetime) -> datetime:
    """Return a UTC-aware datetime, adding UTC tzinfo if the datetime is naive."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

def _parse_last_seen(v: dict, fallback: datetime) -> datetime:
    """Safely parse last_seen from a vessel dict; returns fallback on any error."""
    try:
        return _ensure_aware(datetime.fromisoformat(v.get("last_seen", fallback.isoformat())))
    except (ValueError, TypeError):
        return fallback

# ==========================================
# 📊 ANALYTICS ENGINE
# ==========================================
def update_vessel_timers(active_vessel: Dict, new_status: str, now_utc: datetime) -> Dict:
    current_status   = active_vessel.get("current_status", "UNKNOWN")
    last_updated_str = active_vessel.get("last_updated")

    if last_updated_str:
        try:
            last_updated  = _ensure_aware(datetime.fromisoformat(last_updated_str))
            elapsed_hours = (now_utc - last_updated).total_seconds() / 3600.0

            if current_status in ANCHORAGE_STATUSES:
                active_vessel["anchorage_hours"] = active_vessel.get("anchorage_hours", 0.0) + elapsed_hours
            elif current_status in BERTH_STATUSES:
                active_vessel["berth_hours"] = active_vessel.get("berth_hours", 0.0) + elapsed_hours
        except Exception as e:
            print(f"[WARNING] Timer update failed: {e}")

    active_vessel["current_status"] = new_status
    active_vessel["last_updated"]   = now_utc.isoformat()
    active_vessel["last_seen"]      = now_utc.isoformat()
    return active_vessel

def calculate_performance_note(avg_anchorage: float, avg_berth: float) -> str:
    if avg_anchorage < 5  and avg_berth < 24: return "⭐ Excellent - Opérations rapides"
    if avg_anchorage < 10 and avg_berth < 36: return "✅ Bon - Efficace"
    if avg_anchorage < 24:                    return "⚠️ Modéré - Certaines attentes"
    return "🐌 Lent - Longues périodes d'attente"

# ==========================================
# 📧 EMAIL TEMPLATE — Compact Card Edition
#
# Built entirely with <table> layouts and
# bgcolor attributes so it renders correctly
# in Gmail, Outlook, Apple Mail, and all
# major email clients. No flexbox, no CSS
# gradients, no box-shadow.
# ==========================================
def format_vessel_details_premium(entry: dict) -> str:
    nom       = entry.get("nOM_NAVIREField")    or "INCONNU"
    imo       = entry.get("nUMERO_LLOYDField")  or "N/A"
    cons      = entry.get("cONSIGNATAIREField") or "N/A"
    escale    = entry.get("nUMERO_ESCALEField") or "N/A"
    eta_date  = fmt_dt(entry.get("dATE_SITUATIONField"))
    eta_time  = fmt_time_only(entry.get("hEURE_SITUATIONField"))
    prov      = entry.get("pROVField")          or "Inconnue"
    type_nav  = entry.get("tYP_NAVIREField")    or "N/A"
    p_name    = port_name(str(entry.get("cODE_SOCIETEField", "")))
    generated = datetime.now().strftime("%d/%m/%Y à %H:%M")

    def tile(label: str, value: str, bg: str, border: str) -> str:
        return (
            f'<div style="background:{bg};border-radius:5px;padding:8px 10px;'
            f'border-left:3px solid {border};">'
            f'<div style="font-family:Arial,sans-serif;font-size:8px;color:#7f8c8d;'
            f'text-transform:uppercase;letter-spacing:1px;margin-bottom:3px;">{label}</div>'
            f'<div style="font-family:Arial,sans-serif;font-size:13px;'
            f'font-weight:bold;color:#1a252f;">{value}</div>'
            f'</div>'
        )

    return f"""
    <table width="100%" cellpadding="0" cellspacing="0" border="0"
           style="max-width:460px;margin:16px auto;border:1px solid #d0d9e5;
                  border-radius:8px;overflow:hidden;font-family:Arial,sans-serif;">

      <!-- HEADER -->
      <tr>
        <td colspan="2" style="padding:0;background:#0a3d62;">
          <table width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr>
              <td width="5" bgcolor="#2e86c1"
                  style="background:#2e86c1;">&nbsp;</td>
              <td style="padding:12px 14px;background:#0a3d62;">
                <div style="font-family:Arial,sans-serif;font-size:8px;color:#7ec8e3;
                            letter-spacing:2px;text-transform:uppercase;margin-bottom:4px;">
                  Nouvelle arriv&#233;e pr&#233;vue &middot; {p_name}
                </div>
                <div style="font-family:Arial,sans-serif;font-size:18px;
                            font-weight:bold;color:#ffffff;letter-spacing:0.3px;">
                  {nom}
                </div>
              </td>
              <td width="90" bgcolor="#0a3d62"
                  style="background:#0a3d62;padding-right:14px;
                         text-align:right;vertical-align:middle;">
                <span style="display:inline-block;background:#1e8449;color:#ffffff;
                             font-family:Arial,sans-serif;font-size:9px;font-weight:bold;
                             letter-spacing:1px;padding:3px 9px;border-radius:10px;">
                  PR&#201;VU
                </span>
              </td>
            </tr>
          </table>
        </td>
      </tr>

      <!-- ETA ROW -->
      <tr>
        <td colspan="2" bgcolor="#eaf4fd"
            style="background:#eaf4fd;padding:7px 14px;
                   border-bottom:1px solid #cde0f0;">
          <span style="font-family:Arial,sans-serif;font-size:12px;color:#1a5276;">
            ETA&nbsp;: <strong>{eta_date} &middot; {eta_time}</strong>
          </span>
        </td>
      </tr>

      <!-- IMO + ESCALE -->
      <tr>
        <td width="50%" valign="top"
            style="padding:10px 6px 5px 10px;background:#ffffff;
                   border-bottom:1px solid #edf1f5;">
          {tile("N&ordm;&nbsp;IMO", imo, "#f4f8fc", "#2e86c1")}
        </td>
        <td width="50%" valign="top"
            style="padding:10px 10px 5px 6px;background:#ffffff;
                   border-bottom:1px solid #edf1f5;">
          {tile("N&ordm;&nbsp;Escale", escale, "#f4f8fc", "#2e86c1")}
        </td>
      </tr>

      <!-- TYPE + CONSIGNATAIRE -->
      <tr>
        <td width="50%" valign="top"
            style="padding:5px 6px 5px 10px;background:#ffffff;
                   border-bottom:1px solid #edf1f5;">
          {tile("Type", type_nav, "#fef9f0", "#e67e22")}
        </td>
        <td width="50%" valign="top"
            style="padding:5px 10px 5px 6px;background:#ffffff;
                   border-bottom:1px solid #edf1f5;">
          {tile("Consignataire", cons, "#fef9f0", "#e67e22")}
        </td>
      </tr>

      <!-- PROVENANCE (full width) -->
      <tr>
        <td colspan="2"
            style="padding:5px 10px 10px;background:#ffffff;
                   border-bottom:1px solid #edf1f5;">
          {tile("Provenance", prov, "#f0f9f4", "#1e8449")}
        </td>
      </tr>

      <!-- FOOTER -->
      <tr>
        <td colspan="2" bgcolor="#f7f9fb"
            style="background:#f7f9fb;padding:8px 14px;text-align:center;">
          <span style="font-family:Arial,sans-serif;font-size:10px;color:#95a5a6;">
            ANP Vessel Monitor &nbsp;&middot;&nbsp;
            Alerte automatique g&#233;n&#233;r&#233;e le {generated}
          </span>
        </td>
      </tr>

    </table>"""


def _normalise_history_entry(h: dict) -> dict:
    """Handle both old schema (duration/anchorage_duration) and new
    schema (berth_hours/anchorage_hours). Returns a normalised copy."""
    if "berth_hours" in h:
        return h
    out = dict(h)
    out["berth_hours"]     = h.get("duration",           0.0)
    out["anchorage_hours"] = h.get("anchorage_duration", 0.0)
    return out


def send_monthly_report(history: list, specific_port: str):
    if not history:
        return

    history = [_normalise_history_entry(h) for h in history]

    total_calls = len(history)
    total_anch  = sum(h.get("anchorage_hours", 0) for h in history)
    total_berth = sum(h.get("berth_hours",     0) for h in history)
    avg_anch    = round(total_anch  / total_calls, 1) if total_calls > 0 else 0
    avg_berth   = round(total_berth / total_calls, 1) if total_calls > 0 else 0
    avg_total   = round(avg_anch + avg_berth, 1)

    agent_stats = defaultdict(lambda: {"calls": 0, "total_anch": 0.0, "total_berth": 0.0})
    for h in history:
        agent = h.get("agent", "Inconnu")
        agent_stats[agent]["calls"]       += 1
        agent_stats[agent]["total_anch"]  += h.get("anchorage_hours", 0)
        agent_stats[agent]["total_berth"] += h.get("berth_hours", 0)

    agent_rows = ""
    for agent, data in sorted(agent_stats.items(), key=lambda x: x[1]["calls"], reverse=True):
        a_anch  = round(data["total_anch"]  / data["calls"], 1) if data["calls"] > 0 else 0
        a_berth = round(data["total_berth"] / data["calls"], 1) if data["calls"] > 0 else 0
        note    = calculate_performance_note(a_anch, a_berth)
        a_color = "#e74c3c" if a_anch  > 12 else "#27ae60"
        b_color = "#f39c12" if a_berth > 36 else "#27ae60"
        agent_rows += f"""
        <tr style="border-bottom:1px solid #e0e0e0;">
            <td style="padding:10px;font-weight:bold;">{agent}</td>
            <td style="padding:10px;text-align:center;">{data['calls']}</td>
            <td style="padding:10px;text-align:center;color:{a_color};">{a_anch}h</td>
            <td style="padding:10px;text-align:center;color:{b_color};">{a_berth}h</td>
            <td style="padding:10px;text-align:center;font-size:12px;">{note}</td>
        </tr>"""

    vessel_rows = ""
    for h in sorted(history, key=lambda x: x.get("departure", ""), reverse=True):
        anch  = round(h.get("anchorage_hours", 0), 1)
        berth = round(h.get("berth_hours",     0), 1)
        vessel_rows += f"""
        <tr style="border-bottom:1px solid #f0f0f0;">
            <td style="padding:8px;font-weight:bold;">{h['vessel']}</td>
            <td style="padding:8px;">{h.get('agent', '-')}</td>
            <td style="padding:8px;text-align:center;">{anch}h</td>
            <td style="padding:8px;text-align:center;">{berth}h</td>
            <td style="padding:8px;text-align:center;font-weight:bold;">{round(anch + berth, 1)}h</td>
        </tr>"""

    subject = f"📊 Rapport Mensuel BI : Port de {specific_port} ({total_calls} Escales)"
    body = f"""
    <div style="font-family:Arial;max-width:1100px;margin:auto;">
        <div style="background:#0a3d62;color:white;padding:20px;border-radius:10px 10px 0 0;">
            <h2 style="margin:0;">📊 Business Intelligence Report - {specific_port}</h2>
            <p>{total_calls} escales complétées | Données au {datetime.now().strftime('%d/%m/%Y')}</p>
        </div>
        <div style="background:#f8f9fa;padding:25px;border:1px solid #d0d7e1;
                    border-top:none;border-radius:0 0 10px 10px;">
            <div style="margin-bottom:30px;padding:15px;background:#e8f4fc;
                        border-left:4px solid #3498db;">
                <h3 style="margin:0;color:#2980b9;">📈 KPIs Clés du Port</h3>
                <p><b>Attente Moy.:</b> {avg_anch}h &nbsp;|&nbsp;
                   <b>Quai Moy.:</b> {avg_berth}h &nbsp;|&nbsp;
                   <b>Total Moy.:</b> {avg_total}h</p>
            </div>
            <h3 style="color:#0a3d62;border-bottom:2px solid #0a3d62;">🏢 Performance des Agents</h3>
            <table style="width:100%;border-collapse:collapse;background:white;margin-bottom:30px;">
                <tr style="background:#2c3e50;color:white;">
                    <th style="padding:10px;">Agent</th>
                    <th style="padding:10px;">Escales</th>
                    <th style="padding:10px;">Attente</th>
                    <th style="padding:10px;">Quai</th>
                    <th style="padding:10px;">Note</th>
                </tr>
                {agent_rows}
            </table>
            <h3 style="color:#0a3d62;border-bottom:2px solid #0a3d62;">📋 Statistiques Navires</h3>
            <table style="width:100%;border-collapse:collapse;background:white;font-size:13px;">
                <tr style="background:#ecf0f1;">
                    <th style="padding:8px;">Navire</th>
                    <th style="padding:8px;">Agent</th>
                    <th style="padding:8px;">Attente</th>
                    <th style="padding:8px;">Quai</th>
                    <th style="padding:8px;">Total</th>
                </tr>
                {vessel_rows}
            </table>
        </div>
    </div>"""

    send_email(EMAIL_TO, subject, body)
    if specific_port == "Nador" and EMAIL_TO_COLLEAGUE:
        send_email(EMAIL_TO_COLLEAGUE, subject, body)


def send_email(to: Optional[str], sub: str, body: str):
    """Send an HTML email. Skips silently if disabled, config missing, or no recipient."""
    if not EMAIL_ENABLED or not EMAIL_USER or not to:
        if EMAIL_ENABLED and EMAIL_USER and not to:
            print("[WARNING] send_email called with no recipient — skipping.")
        return
    msg = MIMEText(body, "html", "utf-8")
    msg["Subject"] = sub
    msg["From"]    = EMAIL_USER
    msg["To"]      = to
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASS)
            server.sendmail(EMAIL_USER, [to], msg.as_bytes())
        print(f"[SUCCESS] Email sent to {to}")
    except Exception as e:
        print(f"[ERROR] Email failed: {e}")


# ==========================================
# 🔄 MAIN PROCESS
# ==========================================
def main():
    print(f"{'=' * 50}\n🚢 VESSEL MONITOR - Battle Ready Edition\n{'=' * 50}")
    print(f"MODE: {RUN_MODE.upper()}\nPorts: Safi (03), Nador (06), Jorf Lasfar (07)")

    validate_config()

    state   = load_state()
    active  = state.get("active", {})
    history = state.get("history", [])

    # ── REPORT MODE ──────────────────────────────────────────
    if RUN_MODE == "report":
        for p_code in ALLOWED_PORTS:
            p_name = port_name(p_code)
            p_hist = [h for h in history if h.get("port") == p_name]
            if p_hist:
                send_monthly_report(p_hist, p_name)

        # Archive completed history to file, then clear state
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    old = json.load(f)
                    if isinstance(old, list):
                        history = old + history
            except Exception as e:
                print(f"[WARNING] Could not read history archive: {e}")

        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)

        state["history"] = []
        save_state(state)
        print("[LOG] Monthly reports and archiving completed.")
        return

    # ── MONITOR MODE ─────────────────────────────────────────
    try:
        all_data = fetch_vessel_data_with_retry()
    except Exception as e:
        print(f"[CRITICAL] API Failure: {e}")
        return

    now_utc      = datetime.now(timezone.utc)
    live_vessels = {}

    for e in all_data:
        port_code = str(e.get("cODE_SOCIETEField", ""))
        if port_code in ALLOWED_PORTS:
            status = clean_status(e.get("sITUATIONField"))
            v_id   = f"{e.get('nUMERO_LLOYDField', '0')}-{e.get('nUMERO_ESCALEField', '0')}"
            live_vessels[v_id] = {"e": e, "status": status}

    alerts, to_remove = {}, []

    # ── TRACKING LOOP ────────────────────────────────────────
    for v_id, stored in active.items():
        live = live_vessels.get(v_id)
        if live:
            # Update elapsed time counters
            stored = update_vessel_timers(stored, live["status"], now_utc)

            # Move to history when vessel completes its call
            if live["status"] in COMPLETED_STATUSES:
                history.append({
                    "vessel":          stored["entry"].get("nOM_NAVIREField", "Unknown"),
                    "agent":           stored["entry"].get("cONSIGNATAIREField", "Inconnu"),
                    "port":            port_name(stored["entry"].get("cODE_SOCIETEField")),
                    "anchorage_hours": round(stored.get("anchorage_hours", 0.0), 1),
                    "berth_hours":     round(stored.get("berth_hours",     0.0), 1),
                    "arrival":         stored.get("first_seen", now_utc.isoformat()),
                    "departure":       now_utc.isoformat(),
                })
                to_remove.append(v_id)

            stored["entry"] = live["e"]
        else:
            # Ghost ship: vessel disappeared from API — freeze timers, keep in state briefly.
            # Do NOT update last_seen here; preserving the last API-seen timestamp is what
            # allows the 3-day cutoff below to eventually expire this entry.
            pass

    for vid in to_remove:
        active.pop(vid, None)

    # ── NEW ARRIVALS ─────────────────────────────────────────
    for v_id, live in live_vessels.items():
        if v_id not in active:
            # First-run safety: skip vessels already present that aren't newly planned
            if len(active) == 0 and live["status"] not in PLANNED_STATUSES:
                continue

            active[v_id] = {
                "entry":           live["e"],
                "current_status":  live["status"],
                "anchorage_hours": 0.0,
                "berth_hours":     0.0,
                "first_seen":      now_utc.isoformat(),
                "last_updated":    now_utc.isoformat(),
                "last_seen":       now_utc.isoformat(),
            }
            if live["status"] in PLANNED_STATUSES:
                p = port_name(live["e"].get("cODE_SOCIETEField"))
                alerts.setdefault(p, []).append(live["e"])

    # ── CLEANUP & SAVE ───────────────────────────────────────
    cutoff = now_utc - timedelta(hours=24)
    state["active"] = {
        k: v for k, v in active.items()
        if _parse_last_seen(v, now_utc) > cutoff
    }
    state["history"] = history[-1000:]
    save_state(state)

    # ── SEND ALERTS ──────────────────────────────────────────
    if alerts:
        for p, vessels in alerts.items():
            names = ", ".join(v.get("nOM_NAVIREField", "Unknown") for v in vessels)
            body  = (
                f'<p style="font-family:Arial,sans-serif;font-size:14px;">'
                f'Bonjour,<br>Mouvements pr&#233;vus au Port de <b>{p}</b>&nbsp;:</p>'
                + "".join(format_vessel_details_premium(v) for v in vessels)
            )
            subject = f"NOUVELLE ARRIVÉE | {names} au Port de {p}"
            send_email(EMAIL_TO, subject, body)
            if p == "Nador" and EMAIL_TO_COLLEAGUE:
                send_email(EMAIL_TO_COLLEAGUE, subject, body)

    print(f"[STATS] Tracking {len(state['active'])} vessels | History: {len(history)}")


if __name__ == "__main__":
    main()

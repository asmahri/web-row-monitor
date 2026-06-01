"""
migrate.py — One-time data migration for ANP Vessel Monitor
============================================================
Run ONCE before deploying the updated monitor.py.

What it does
------------
1. history.json  — renames old schema fields to new names:
                   duration          → berth_hours
                   anchorage_duration → anchorage_hours
                   adds arrival       (departure minus berth_hours)

2. state.json active dict:
   a. Flushes all APPAREILLAGE / TERMINE vessels to state history
      (763 stuck completed vessels caused by the ghost-expiry bug)
   b. Resets stale last_updated → last_seen on remaining entries
      (prevents up to 3168 fake hours being added on next API hit)
   c. Removes legacy 'status' field (replaced by 'current_status')
   d. Fills missing field defaults (first_seen, anchorage_hours,
      berth_hours, current_status)

3. Trims combined state history to the most recent 1000 entries.

Usage
-----
  python migrate.py                        # dry-run (prints only)
  python migrate.py --apply               # writes files in-place
  python migrate.py --apply --path ./data # custom directory
"""

import json
import shutil
import argparse
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Status constants (must match monitor.py) ──────────────────
COMPLETED_STATUSES = {"APPAREILLAGE", "TERMINE"}
VALID_STATUSES     = {"PREVU", "EN RADE", "A QUAI", "APPAREILLAGE", "TERMINE"}


# ── Helpers ───────────────────────────────────────────────────
def parse_dt(s: str) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None

def port_name(code: str) -> str:
    return {"03": "Safi", "06": "Nador", "07": "Jorf Lasfar"}.get(str(code), f"Port {code}")

def backup_and_write(path: Path, data, apply: bool):
    if apply:
        backup = path.with_suffix(path.suffix + ".pre-migration")
        shutil.copy2(path, backup)
        print(f"  ✓ Backup → {backup.name}")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"  ✓ Written → {path.name}")
    else:
        print(f"  [dry-run] Would write {path.name}")


# ── 1. Migrate history.json ───────────────────────────────────
def migrate_history(history_path: Path, apply: bool) -> list:
    print(f"\n{'='*52}")
    print("STEP 1 — Migrate history.json schema")
    print(f"{'='*52}")

    with open(history_path, encoding="utf-8") as f:
        records = json.load(f)

    print(f"  Records loaded  : {len(records)}")

    already_new    = sum(1 for r in records if "berth_hours"   in r)
    already_old    = sum(1 for r in records if "duration"      in r)
    has_arrival    = sum(1 for r in records if "arrival"       in r)
    print(f"  Old schema      : {already_old}")
    print(f"  Already migrated: {already_new}")
    print(f"  Has arrival     : {has_arrival}")

    migrated = []
    changed = 0
    for r in records:
        entry = dict(r)

        # Rename fields if still on old schema
        if "duration" in entry and "berth_hours" not in entry:
            entry["berth_hours"] = entry.pop("duration")
            changed += 1
        if "anchorage_duration" in entry and "anchorage_hours" not in entry:
            entry["anchorage_hours"] = entry.pop("anchorage_duration")

        # Ensure anchorage_hours exists
        entry.setdefault("anchorage_hours", 0.0)
        entry.setdefault("berth_hours",     0.0)

        # Add arrival estimate if missing
        if "arrival" not in entry and entry.get("departure"):
            dep_dt = parse_dt(entry["departure"])
            if dep_dt:
                total_h = entry["berth_hours"] + entry["anchorage_hours"]
                arr_dt  = dep_dt - timedelta(hours=max(total_h, 0))
                entry["arrival"] = arr_dt.isoformat()

        migrated.append(entry)

    print(f"  Fields renamed  : {changed}")
    backup_and_write(history_path, migrated, apply)
    return migrated


# ── 2. Migrate state.json ─────────────────────────────────────
def migrate_state(state_path: Path, apply: bool):
    print(f"\n{'='*52}")
    print("STEP 2 — Migrate state.json")
    print(f"{'='*52}")

    with open(state_path, encoding="utf-8") as f:
        state = json.load(f)

    active      = state.get("active",  {})
    int_history = state.get("history", [])
    now_utc     = datetime.now(timezone.utc)
    cutoff_stale = now_utc - timedelta(hours=24)  # matches new monitor.py cutoff

    print(f"  Active entries  : {len(active)}")
    print(f"  History entries : {len(int_history)}")

    flushed     = 0
    reset_timer = 0
    cleaned     = 0
    to_flush    = []
    to_keep     = {}

    for v_id, v in active.items():
        status = v.get("current_status", v.get("status", "UNKNOWN"))
        entry  = v.get("entry", {})

        # ── a. Flush completed vessels to history ──────────────
        if status in COMPLETED_STATUSES:
            last_seen = parse_dt(v.get("last_seen", "")) or now_utc
            int_history.append({
                "vessel":          entry.get("nOM_NAVIREField",    "Unknown"),
                "agent":           entry.get("cONSIGNATAIREField", "Inconnu"),
                "port":            port_name(entry.get("cODE_SOCIETEField", "")),
                "anchorage_hours": round(v.get("anchorage_hours", 0.0), 1),
                "berth_hours":     round(v.get("berth_hours",     0.0), 1),
                "arrival":         v.get("first_seen", v.get("last_updated", last_seen.isoformat())),
                "departure":       last_seen.isoformat(),
            })
            flushed += 1
            continue

        # ── b. Clean up remaining active entries ───────────────
        clean = dict(v)

        # Remove legacy 'status' field
        if "status" in clean:
            clean.pop("status")
            cleaned += 1

        # Fill missing defaults
        clean.setdefault("current_status",  status if status in VALID_STATUSES else "UNKNOWN")
        clean.setdefault("anchorage_hours", 0.0)
        clean.setdefault("berth_hours",     0.0)
        clean.setdefault("first_seen",      clean.get("last_updated", now_utc.isoformat()))

        # ── c. Reset stale last_updated ────────────────────────
        last_upd = parse_dt(clean.get("last_updated", ""))
        if last_upd is None or last_upd < cutoff_stale:
            # Use last_seen as the new baseline to preserve relative recency
            clean["last_updated"] = clean.get("last_seen", now_utc.isoformat())
            reset_timer += 1

        to_keep[v_id] = clean

    print(f"\n  Flushed completed → history : {flushed}")
    print(f"  Stale timers reset          : {reset_timer}")
    print(f"  Legacy 'status' removed     : {cleaned}")
    print(f"  Remaining active            : {len(to_keep)}")

    # ── Trim combined history to 1000 ─────────────────────────
    # Sort by departure descending, keep freshest
    def dep_key(h):
        return h.get("departure", "")

    int_history_sorted = sorted(int_history, key=dep_key, reverse=True)
    trimmed = len(int_history_sorted) - 1000
    int_history_final  = int_history_sorted[:1000]
    print(f"\n  Combined history            : {len(int_history)}")
    print(f"  Trimmed (oldest)            : {max(trimmed, 0)}")
    print(f"  Final history               : {len(int_history_final)}")

    state["active"]  = to_keep
    state["history"] = int_history_final

    backup_and_write(state_path, state, apply)
    return state


# ── Main ──────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="ANP Vessel Monitor — data migration")
    parser.add_argument("--apply", action="store_true",
                        help="Write changes to disk (default: dry-run)")
    parser.add_argument("--path", default=".",
                        help="Directory containing state.json and history.json")
    args = parser.parse_args()

    base         = Path(args.path)
    state_path   = base / "state.json"
    history_path = base / "history.json"

    for p in (state_path, history_path):
        if not p.exists():
            print(f"[FATAL] {p} not found — aborting.")
            sys.exit(1)

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"\n{'='*52}")
    print(f"  ANP Vessel Monitor — Data Migration")
    print(f"  Mode: {mode}")
    print(f"{'='*52}")

    migrate_history(history_path, args.apply)
    migrate_state(state_path, args.apply)

    print(f"\n{'='*52}")
    if args.apply:
        print("  ✅ Migration complete.")
        print("  Pre-migration backups saved as *.pre-migration")
        print("  Deploy updated monitor.py and push state.json +")
        print("  history.json to your repo.")
    else:
        print("  Dry-run complete — no files written.")
        print("  Re-run with --apply to commit changes.")
    print(f"{'='*52}\n")


if __name__ == "__main__":
    main()

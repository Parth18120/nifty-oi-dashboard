"""Event calendar (RBI/FOMC/CPI/expiry) lookups and overnight-gap statistics."""

import csv
import json
from datetime import datetime
from pathlib import Path

from oi_dashboard.core import state
from oi_dashboard.core.constants import IST, SPOT_HISTORY_FILE
from oi_dashboard.core.paths import ROOT
from oi_dashboard.core.util import parse_date_safe


def load_events():
    """Load data/events.json into _events_map and _events_list."""
    events_path = Path(str(ROOT)) / "data" / "events.json"
    if not events_path.exists():
        print("[EVENTS] data/events.json not found — no event overrides")
        return
    try:
        with open(events_path, encoding="utf-8") as f:
            raw = json.load(f)
        state.events_list = raw
        # Build map: per day, highest-priority action wins (SKIP > IC_ONLY > REDUCED)
        _priority = {"SKIP": 3, "IC_ONLY": 2, "REDUCED": 1}
        for ev in raw:
            d = ev.get("date", "")
            if not d:
                continue
            existing = state.events_map.get(d)
            if existing is None or _priority.get(ev["action"], 0) > _priority.get(existing["action"], 0):
                state.events_map[d] = ev
        print(f"[EVENTS] Loaded {len(state.events_list)} events, {len(state.events_map)} unique dates")
    except Exception as e:
        print(f"[EVENTS] Failed to load events.json: {e}")


def get_today_event():
    """Returns event dict for today or None."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    return state.events_map.get(today)


def get_upcoming_event(days=3):
    """Returns the next event within `days` trading days, or None."""
    today = datetime.now(IST).date()
    upcoming = []
    for ev in state.events_list:
        try:
            d = datetime.strptime(ev["date"], "%Y-%m-%d").date()
            diff = (d - today).days
            if 1 <= diff <= days:
                upcoming.append((diff, ev))
        except (ValueError, KeyError):
            continue
    if not upcoming:
        return None
    upcoming.sort(key=lambda x: x[0])
    days_away, ev = upcoming[0]
    return {**ev, "days_away": days_away}


def compute_gap_stats():
    if not SPOT_HISTORY_FILE or not Path(SPOT_HISTORY_FILE).exists():
        return
    try:
        rows = []
        with open(SPOT_HISTORY_FILE, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                d = parse_date_safe(row.get("date", "") or row.get("datetime", ""))
                if d is None:
                    continue
                try:
                    rows.append({"date": d.isoformat(), "dt": d,
                                 "open": float(row.get("open",0) or 0),
                                 "close": float(row.get("close",0) or 0)})
                except (ValueError, TypeError):
                    continue
        rows.sort(key=lambda r: r["dt"])
        gaps = []
        for i in range(1, len(rows)):
            pc, to = rows[i-1]["close"], rows[i]["open"]
            if pc > 0 and to > 0:
                gaps.append(abs((to - pc) / pc * 100))
        if not gaps:
            return
        gaps.sort()
        n = len(gaps)
        def _pct(p): return gaps[min(int(n * p / 100), n-1)]
        state.gap_stats = {
            "count":  n,
            "p50":    round(_pct(50), 2),
            "p90":    round(_pct(90), 2),
            "p95":    round(_pct(95), 2),
            "worst":  round(gaps[-1], 2),
        }
        print(f"[GAP] {n} days: p50={state.gap_stats['p50']}% p90={state.gap_stats['p90']}% worst={state.gap_stats['worst']}%")
    except Exception as e:
        print(f"[GAP] Error: {e}")

"""Intraday spot analytics: rolling RV, choppiness, micro-moves and spike API payloads."""

import math
from datetime import datetime, timedelta

from oi_dashboard.core import state
from oi_dashboard.core.constants import IST
from oi_dashboard.core.paths import ROOT


def get_spot_range(minutes=60):
    """Returns spot high-low range over last N minutes."""
    if len(state.spot_60min_history) < 2:
        return 999
    now_ist = datetime.now(IST)
    cutoff  = now_ist - timedelta(minutes=minutes)
    recent  = [s for t, s in state.spot_60min_history if t >= cutoff]
    if len(recent) < 2:
        return 999
    return round(max(recent) - min(recent), 1)


def update_minute_history(now_dt, price):
    """Append (timestamp, price) to _minute_history; keep last 95 minutes."""
    state.minute_history.append((now_dt, float(price)))
    cutoff = now_dt - timedelta(minutes=95)
    state.minute_history[:] = [(t, p) for t, p in state.minute_history if t > cutoff]


def compute_rolling_rv(window_minutes=15):
    """Annualized vol over last N minutes of _minute_history. Returns % or None."""
    if len(state.minute_history) < 5:
        return None
    cutoff = datetime.now(IST) - timedelta(minutes=window_minutes)
    bars   = [(t, p) for t, p in state.minute_history if t >= cutoff]
    if len(bars) < 4:
        return None
    rets = [math.log(bars[i][1] / bars[i-1][1])
            for i in range(1, len(bars)) if bars[i-1][1] > 0]
    if len(rets) < 3:
        return None
    mean_r = sum(rets) / len(rets)
    var    = sum((r - mean_r) ** 2 for r in rets) / (len(rets) - 1)
    annual = math.sqrt(var * 375 * 252) * 100   # 375 min/day trading
    return round(annual, 2)


def compute_rv_sparkline():
    """
    Return list of {"time": "HH:MM", "rv": float} for last 90 min,
    one point per refresh interval (sparse is fine — JS fills gaps).
    Uses a rolling 15-min window ending at each recorded bar.
    """
    series = []
    bars   = state.minute_history[:]   # snapshot
    if len(bars) < 6:
        return series
    for i in range(4, len(bars)):
        t_end   = bars[i][0]
        t_start = t_end - timedelta(minutes=15)
        window  = [(t, p) for t, p in bars[:i+1] if t >= t_start]
        if len(window) < 4:
            continue
        rets = [math.log(window[j][1] / window[j-1][1])
                for j in range(1, len(window)) if window[j-1][1] > 0]
        if len(rets) < 3:
            continue
        mean_r = sum(rets) / len(rets)
        var    = sum((r - mean_r) ** 2 for r in rets) / (len(rets) - 1)
        annual = math.sqrt(var * 375 * 252) * 100
        series.append({"time": t_end.strftime("%H:%M"), "rv": round(annual, 2)})
    return series


def detect_micro_move():
    """
    Return dict if spot moved >= MICRO_MOVE_PT_THRESHOLD in last 5 bars, else None.
    A "bar" = one server refresh (typically 60s).
    """
    if len(state.minute_history) < 5:
        return None
    recent    = state.minute_history[-5:]
    move      = recent[-1][1] - recent[0][1]
    move_pts  = abs(move)
    ref_price = recent[0][1]
    threshold = 40   # points — hardcoded; config can override later
    if move_pts < threshold:
        return None
    return {
        "time":        recent[-1][0],
        "move_pts":    round(move, 1),
        "move_pct":    round(move_pts / ref_price * 100, 3),
        "direction":   "UP" if move > 0 else "DOWN",
        "start_price": round(ref_price, 1),
        "end_price":   round(recent[-1][1], 1),
    }


def compute_choppiness(window_minutes=30):
    """
    Dreiss Choppiness Index: 100 * log10(sum_atr / (H - L)) / log10(n)
    Scale 0-100. Low = trending, high = choppy.
      < 38.2  → TRENDING     (clean directional tape, ideal for spreads)
      38.2-50 → MILD_TREND   (leaning one direction)
      50-61.8 → NEUTRAL
      61.8-75 → CHOPPY       (whipsaw risk)
      > 75    → EXTREME_CHOP (do not enter)
    """
    import math as _math
    if len(state.minute_history) < 5:
        return None, "WAIT"
    cutoff = datetime.now(IST) - timedelta(minutes=window_minutes)
    bars   = [p for t, p in state.minute_history if t >= cutoff]
    if len(bars) < 4:
        return None, "WAIT"
    n          = len(bars)
    sum_atr    = sum(abs(bars[i] - bars[i-1]) for i in range(1, n))
    hl_range   = max(bars) - min(bars)
    if hl_range < 0.5 or sum_atr < 0.01:
        return None, "WAIT"
    chop = round(100 * _math.log10(sum_atr / hl_range) / _math.log10(n), 2)
    chop = max(0.0, min(100.0, chop))
    if   chop < 38.2: label = "TRENDING"
    elif chop < 50.0: label = "MILD_TREND"
    elif chop < 61.8: label = "NEUTRAL"
    elif chop < 75.0: label = "CHOPPY"
    else:             label = "EXTREME_CHOP"
    return chop, label


def update_rv_peak(rv_now):
    """Track today's rolling-RV peak; update global."""
    if rv_now and rv_now > state.rv_peak_today:
        state.rv_peak_today      = rv_now
        state.rv_peak_time_today = datetime.now(IST).strftime("%H:%M")


def run_micro_move_detection(now_dt):
    """Check for a micro move and log it (dedup by 5-min window)."""
    event = detect_micro_move()
    if not event:
        return
    # Dedup: skip if we logged an event within the last 5 minutes
    if state.micro_moves_today:
        last_t = state.micro_moves_today[-1]["time"]
        if (now_dt - last_t).total_seconds() < 300:
            return
    state.micro_moves_today.append(event)
    print(f"[MICRO MOVE] {event['direction']} {event['move_pts']:+.0f}pt in ~5min "
          f"({event['start_price']}→{event['end_price']})")


def save_minute_bars_eod():
    """Save today's minute history to data/minute_bars/YYYY-MM-DD.csv after market close."""
    if state.minute_bars_saved or not state.minute_history:
        return
    try:
        import os as _os
        today    = datetime.now(IST).strftime("%Y-%m-%d")
        out_dir  = _os.path.join(str(ROOT), "data", "minute_bars")
        _os.makedirs(out_dir, exist_ok=True)
        out_path = _os.path.join(out_dir, f"{today}.csv")
        with open(out_path, "w") as f:
            f.write("timestamp,close\n")
            for t, p in state.minute_history:
                f.write(f"{t.isoformat()},{p}\n")
        print(f"[EOD] saved {len(state.minute_history)} minute bars to {out_path}")
        state.minute_bars_saved = True
    except Exception as e:
        print(f"[EOD] minute bar save failed: {e}")


def build_spike_api():
    """Serialize active spike for JSON API (converts datetimes to strings)."""
    try:
        from oi_dashboard.analytics import spike_detector as _sd
        spike = _sd.get_active_spike()
        if spike is None:
            return None
        return {
            "detected_at":   spike["detected_at"].strftime("%H:%M:%S") if hasattr(spike["detected_at"], "strftime") else str(spike["detected_at"]),
            "direction":     spike["direction"],
            "move_pts":      spike["move_pts"],
            "move_pct":      spike["move_pct"],
            "verdict":       spike["verdict"],
            "confidence":    spike["confidence"],
            "active_until":  spike["active_until"].strftime("%H:%M") if hasattr(spike.get("active_until"), "strftime") else "",
            "sl_paused":     _sd.is_sl_paused(),
            "iv_suspended":  _sd.is_iv_crush_suspended(),
            "entry_blocked": _sd.is_entry_blocked(),
            "signals":       [{"name": n, "verdict": v, "note": note} for n, v, note in spike.get("signals", [])],
            "actions":       spike.get("action_taken", []),
        }
    except Exception:
        return None


def build_spike_log_api(limit=20):
    """Serialize spike log for /api/spike_log."""
    try:
        from oi_dashboard.analytics import spike_detector as _sd
        log = _sd.get_spike_log(limit)
        result = []
        for s in log:
            result.append({
                "detected_at":    s["detected_at"].strftime("%H:%M:%S") if hasattr(s["detected_at"], "strftime") else str(s["detected_at"]),
                "direction":      s["direction"],
                "move_pts":       s["move_pts"],
                "move_pct":       s["move_pct"],
                "verdict":        s["verdict"],
                "confidence":     s["confidence"],
                "signals":        [{"name": n, "verdict": v, "note": note} for n, v, note in s.get("signals", [])],
                "price_plus_15m": s.get("price_plus_15min"),
                "price_plus_60m": s.get("price_plus_60min"),
                "actions":        s.get("action_taken", []),
            })
        return result
    except Exception:
        return []

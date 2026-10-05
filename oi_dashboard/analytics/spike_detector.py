"""
spike_detector.py
Detects sudden intraday NIFTY price spikes and classifies them as either:
  LIKELY_MICROSTRUCTURE — liquidity-driven, expect mean-reversion
  LIKELY_NEWS           — information-driven, directional move
  LEANS_* / UNCERTAIN   — mixed signals

Used by the trading engine to optionally pause stop-loss exits on open trades
during probable liquidity events.
"""

from datetime import datetime, timedelta
from datetime import time as dtime
from statistics import mean

try:
    from config import (
        SPIKE_ACTIVE_DURATION_MIN,
        SPIKE_MOVE_PCT_THRESHOLD,
        SPIKE_MOVE_WINDOW_MIN,
        SPIKE_SL_PAUSE_MIN,
        SPIKE_VERDICT_MIN_CONFIDENCE,
    )
except ImportError:
    SPIKE_MOVE_PCT_THRESHOLD      = 0.4
    SPIKE_MOVE_WINDOW_MIN         = 15
    SPIKE_ACTIVE_DURATION_MIN     = 30
    SPIKE_SL_PAUSE_MIN            = 15
    SPIKE_VERDICT_MIN_CONFIDENCE  = 4

from datetime import timedelta as _td
from datetime import timezone

IST = timezone(_td(hours=5, minutes=30))

# ── Rolling state ────────────────────────────────────────────────────────────
_minute_bars: list = []         # last 30 NIFTY 1-min bars
_vix_history: list = []         # last 60 min of (timestamp, vix) readings
_active_spike: dict | None = None
_spike_log:   list = []         # all detected spikes this session


# ── State updates (called each refresh from market.snapshot) ───────────────────────

def update_minute_bars(bars: list):
    """Replace minute bar history with latest batch from dhan_feed."""
    global _minute_bars
    _minute_bars = list(bars)[-30:]


def update_vix_history(vix_now: float, ts=None):
    """Append current VIX reading; prune entries older than 60 min."""
    global _vix_history
    if not (vix_now and vix_now > 0):
        return
    if ts is None:
        ts = datetime.now(IST)
    _vix_history.append({"ts": ts, "vix": float(vix_now)})
    cutoff = ts - timedelta(minutes=60)
    _vix_history = [v for v in _vix_history if v["ts"] > cutoff]


# ── Spike trigger ────────────────────────────────────────────────────────────

def check_spike_trigger():
    """Return spike dict if rolling N-min move exceeds threshold, else None."""
    window = SPIKE_MOVE_WINDOW_MIN
    if len(_minute_bars) < window:
        return None

    start_close = _minute_bars[-window]["close"]
    end_close   = _minute_bars[-1]["close"]
    if start_close <= 0:
        return None

    move_pct = (end_close - start_close) / start_close * 100
    move_pts = end_close - start_close

    if abs(move_pct) < SPIKE_MOVE_PCT_THRESHOLD:
        return None

    return {
        "detected_at": _minute_bars[-1]["time"],
        "start_close": round(start_close, 2),
        "end_close":   round(end_close,   2),
        "move_pts":    round(move_pts, 2),
        "move_pct":    round(move_pct, 2),
        "direction":   "DOWN" if move_pts < 0 else "UP",
    }


# ── Six checks ───────────────────────────────────────────────────────────────

def _check_time_of_day():
    now_t = datetime.now(IST).time()
    lunch_s = dtime(12, 30); lunch_e = dtime(13, 30)
    open_e  = dtime(11, 0);  open_s  = dtime(9, 15)
    close_s = dtime(14, 30); close_e = dtime(15, 30)
    if lunch_s <= now_t <= lunch_e:
        return "MICRO",   f"lunch hour {now_t.strftime('%H:%M')} — thin book expected"
    if open_s <= now_t <= open_e:
        return "NEWS",    f"opening hours {now_t.strftime('%H:%M')} — real flow common"
    if close_s <= now_t <= close_e:
        return "NEWS",    f"closing session {now_t.strftime('%H:%M')} — position squaring"
    return "NEUTRAL", f"mid-session {now_t.strftime('%H:%M')}"


def _check_scheduled_event(events_today):
    if events_today:
        evt = events_today[0]
        label = evt.get("label") or evt.get("type") or "event"
        return "NEWS", f"event today: {label} — real news likely"
    return "MICRO", "no scheduled event today"


def _check_banknifty(spike, banknifty_now, banknifty_prev):
    if not (banknifty_now and banknifty_prev and banknifty_prev > 0):
        return "SKIP", "BankNifty data unavailable"
    nifty_move  = spike["move_pct"]
    bn_move_pct = (banknifty_now - banknifty_prev) / banknifty_prev * 100
    ratio = abs(bn_move_pct) / abs(nifty_move) if nifty_move else 0
    if ratio < 0.5:
        return "MICRO", (f"NIFTY {nifty_move:+.2f}% but BN only {bn_move_pct:+.2f}% "
                         f"(ratio {ratio:.2f}) — NIFTY-specific")
    if ratio > 0.8:
        return "NEWS",  f"BN moved {bn_move_pct:+.2f}% in sync (ratio {ratio:.2f}) — macro flow"
    return "NEUTRAL", f"BN {bn_move_pct:+.2f}%, ratio {ratio:.2f} — partial correlation"


def _check_usdinr(usdinr_now, usdinr_prev):
    if not (usdinr_now and usdinr_prev and usdinr_prev > 0):
        return "SKIP", "USD/INR data unavailable"
    fx_move_bps = (usdinr_now - usdinr_prev) * 100
    if abs(fx_move_bps) < 3:
        return "MICRO", f"USD/INR barely moved ({fx_move_bps:+.1f} bps) — no FX flow"
    return "NEWS",  f"USD/INR {fx_move_bps:+.1f} bps — cross-asset move confirmed"


def _check_volume(spike):
    if len(_minute_bars) < 20:
        return "SKIP", "insufficient bar history"
    spike_vol = _minute_bars[-1]["volume"]
    avg_vol   = mean(b["volume"] for b in _minute_bars[-20:-1]) or 1
    ratio     = spike_vol / avg_vol
    if ratio > 3.0:
        sustained = sum(1 for b in _minute_bars[-5:] if b["volume"] > avg_vol * 2) >= 3
        if sustained:
            return "NEWS",  f"vol {ratio:.1f}× sustained over 5 bars — institutional flow"
        return "MICRO", f"vol {ratio:.1f}× single-bar spike — flush/stop-hunt pattern"
    if ratio > 1.5:
        return "NEUTRAL", f"vol {ratio:.1f}× avg — mild elevation"
    return "MICRO", f"vol {ratio:.1f}× — no volume confirmation for {spike['move_pct']:+.2f}% move"


def _check_vix(spike, vix_now, vix_15min_ago):
    if not (vix_now and vix_15min_ago and vix_15min_ago > 0):
        return "SKIP", "insufficient VIX history"
    vix_chg = (vix_now - vix_15min_ago) / vix_15min_ago * 100
    if vix_chg > 20:
        return "NEWS",    f"VIX +{vix_chg:.1f}% — real fear spike"
    if 10 <= vix_chg <= 20:
        return "NEUTRAL", f"VIX +{vix_chg:.1f}% — moderate vol response"
    return "MICRO", (f"VIX only {vix_chg:+.1f}% for "
                     f"{spike['move_pct']:+.2f}% NIFTY move — muted volatility")


def run_six_checks(spike, banknifty_now, banknifty_prev,
                   usdinr_now, usdinr_prev, vix_now, vix_15min_ago, events_today):
    """Return list of (name, verdict, note) for all 6 signals."""
    return [
        ("time_of_day",      *_check_time_of_day()),
        ("scheduled_event",  *_check_scheduled_event(events_today)),
        ("banknifty",        *_check_banknifty(spike, banknifty_now, banknifty_prev)),
        ("usdinr",           *_check_usdinr(usdinr_now, usdinr_prev)),
        ("volume",           *_check_volume(spike)),
        ("vix",              *_check_vix(spike, vix_now, vix_15min_ago)),
    ]


def aggregate_verdict(signals):
    """Return (verdict_str, confidence_count)."""
    micro = sum(1 for _, v, _ in signals if v == "MICRO")
    news  = sum(1 for _, v, _ in signals if v == "NEWS")
    total = micro + news + sum(1 for _, v, _ in signals if v == "NEUTRAL")

    if total == 0:
        return "INDETERMINATE", 0
    min_conf = SPIKE_VERDICT_MIN_CONFIDENCE
    if micro >= min_conf and micro > news:
        return "LIKELY_MICROSTRUCTURE", micro
    if news  >= min_conf and news  > micro:
        return "LIKELY_NEWS", news
    if micro > news:
        return "LEANS_MICROSTRUCTURE", micro
    if news  > micro:
        return "LEANS_NEWS", news
    return "UNCERTAIN", 0


# ── Main entry point ─────────────────────────────────────────────────────────

def detect_and_classify(spot_now, vix_now, banknifty_now, banknifty_prev,
                         usdinr_now, usdinr_prev, events_today):
    """
    Called every refresh. Returns spike result dict if a spike is detected,
    else None. Also updates _active_spike state.
    """
    global _active_spike, _spike_log

    spike = check_spike_trigger()
    if spike is None:
        return None

    # De-duplicate: don't re-fire the same spike within 5 minutes
    if _active_spike:
        secs_since = (datetime.now(IST) - _active_spike["detected_at"]).total_seconds()
        if secs_since < 300:
            return None   # still within previous spike window

    # VIX 15 min ago from history
    vix_15min_ago = None
    target_ts = datetime.now(IST) - timedelta(minutes=15)
    for v in reversed(_vix_history):
        if v["ts"] <= target_ts:
            vix_15min_ago = v["vix"]
            break
    if vix_15min_ago is None and _vix_history:
        vix_15min_ago = _vix_history[0]["vix"]

    signals = run_six_checks(
        spike, banknifty_now, banknifty_prev,
        usdinr_now, usdinr_prev,
        vix_now, vix_15min_ago,
        events_today,
    )
    verdict, confidence = aggregate_verdict(signals)

    now = datetime.now(IST)
    result = {
        **spike,
        "signals":           signals,
        "verdict":           verdict,
        "confidence":        confidence,
        "active_until":      now + timedelta(minutes=SPIKE_ACTIVE_DURATION_MIN),
        "sl_pause_until":    now + timedelta(minutes=SPIKE_SL_PAUSE_MIN),
        "price_plus_15min":  None,   # filled in later
        "price_plus_60min":  None,
        "action_taken":      [],
    }

    _active_spike = result
    _spike_log.append(result)
    return result


# ── Active spike queries ─────────────────────────────────────────────────────

def get_active_spike():
    """Return active spike dict if still within window, else None."""
    global _active_spike
    if _active_spike is None:
        return None
    if datetime.now(IST) > _active_spike["active_until"]:
        _active_spike = None
    return _active_spike


def is_sl_paused():
    """True if active spike is LIKELY_MICROSTRUCTURE and SL pause window active."""
    spike = get_active_spike()
    if spike is None:
        return False
    if spike["verdict"] != "LIKELY_MICROSTRUCTURE":
        return False
    return datetime.now(IST) <= spike["sl_pause_until"]


def is_entry_blocked():
    """True if any active spike (all verdicts block new entries)."""
    return get_active_spike() is not None


def is_iv_crush_suspended():
    """IV_CRUSH exit suspended during LIKELY_MICROSTRUCTURE — IV will rebound."""
    spike = get_active_spike()
    if spike is None:
        return False
    return spike["verdict"] == "LIKELY_MICROSTRUCTURE"


def record_action(action_str: str):
    """Append an action note to the active spike (for dashboard display)."""
    if _active_spike:
        _active_spike.setdefault("action_taken", []).append(action_str)


def update_spike_follow_prices(spot_now):
    """
    Called each refresh. Fills in price_plus_15min / price_plus_60min
    on the active spike for post-hoc mean-reversion tracking.
    """
    if _active_spike is None:
        return
    detected = _active_spike["detected_at"]
    now      = datetime.now(IST)
    elapsed  = (now - detected).total_seconds()
    if _active_spike.get("price_plus_15min") is None and elapsed >= 900:
        _active_spike["price_plus_15min"] = spot_now
    if _active_spike.get("price_plus_60min") is None and elapsed >= 3600:
        _active_spike["price_plus_60min"] = spot_now


# ── Spike log ────────────────────────────────────────────────────────────────

def get_spike_log(limit=20):
    return _spike_log[-limit:]


def get_spike_stats():
    """
    Backtest accuracy report after ≥20 spikes.
    Returns None if fewer than 20 spikes accumulated.
    """
    if len(_spike_log) < 20:
        return None
    micro_spikes = [s for s in _spike_log if s["verdict"] == "LIKELY_MICROSTRUCTURE"]
    news_spikes  = [s for s in _spike_log if s["verdict"] == "LIKELY_NEWS"]

    def mean_reverted(spike):
        p0   = spike["end_close"]
        p60  = spike["price_plus_60min"]
        if p0 is None or p60 is None:
            return None
        # Mean-reverted if 60-min price moved ≥50% back toward start_close
        start = spike["start_close"]
        moved_back = abs(p60 - p0) / abs(p0 - start) if abs(p0 - start) > 0 else 0
        return moved_back >= 0.5

    micro_rev = [s for s in micro_spikes if mean_reverted(s) is True]
    news_dir  = [s for s in news_spikes  if mean_reverted(s) is False]
    return {
        "total":            len(_spike_log),
        "micro_total":      len(micro_spikes),
        "micro_reverted":   len(micro_rev),
        "micro_accuracy":   round(len(micro_rev) / len(micro_spikes) * 100, 1) if micro_spikes else None,
        "news_total":       len(news_spikes),
        "news_directional": len(news_dir),
        "news_accuracy":    round(len(news_dir) / len(news_spikes) * 100, 1)   if news_spikes  else None,
    }

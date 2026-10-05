"""
deep_otm_signal.py — Deep OTM IV Inflation Signal

Detects when far-OTM PE or CE options trade at abnormally high IV vs ATM,
typically caused by institutional put-buying / tail-hedge demand.

Separate from the sweet-zone Iron Condor strategy — this fires in the
1000-1500pt OTM band and always uses a hedged spread structure.

Can co-exist with an open Iron Condor (different regime, different strikes).
"""

import math
from datetime import datetime, timedelta, timezone

_IST = timezone(timedelta(hours=5, minutes=30))


# ── IV Inflation detection ────────────────────────────────────────────────────

def compute_iv_inflation(chain, spot, side):
    """
    For each far-OTM strike (>500pt away), compute how many percentage-points
    its IV is above ATM IV.

    Returns list of dicts sorted by inflation_abs descending, or [] if no ATM row.
    """
    if not chain or not spot:
        return []

    side_lc = side.lower()     # "pe" or "ce"
    iv_key  = f"{side_lc}_iv"
    ltp_key = f"{side_lc}_ltp"

    atm_strike = round(spot / 50) * 50
    atm_row    = next((r for r in chain if r["strike"] == atm_strike), None)
    if atm_row is None:
        # try nearest available
        atm_row = min(chain, key=lambda r: abs(r["strike"] - spot))
    atm_iv = float(atm_row.get(iv_key) or 0)
    if atm_iv <= 0:
        return []

    results = []
    for row in chain:
        s        = row["strike"]
        distance = abs(s - spot)
        if distance < 500:
            continue
        if side == "PE" and s >= spot:
            continue
        if side == "CE" and s <= spot:
            continue

        strike_iv = float(row.get(iv_key) or 0)
        ltp       = float(row.get(ltp_key) or 0)
        if strike_iv <= 0 or ltp <= 0:
            continue

        infl_abs = strike_iv - atm_iv
        infl_pct = (infl_abs / atm_iv * 100) if atm_iv > 0 else 0
        results.append({
            "strike":        s,
            "distance":      distance,
            "iv":            round(strike_iv, 2),
            "atm_iv":        round(atm_iv, 2),
            "inflation_abs": round(infl_abs, 2),
            "inflation_pct": round(infl_pct, 1),
            "ltp":           round(ltp, 2),
        })

    return sorted(results, key=lambda x: x["inflation_abs"], reverse=True)


# ── Simple IV-persistence tracker ────────────────────────────────────────────
# Tracks last N inflation readings per strike to detect expanding vs contracting.

_iv_history = {}   # {(strike, side): [(timestamp, iv), ...]}  — last 20 snapshots


def record_iv_snapshot(chain, spot):
    """Call once per refresh to record per-strike IV for persistence tracking."""
    now = datetime.now(_IST)
    for row in chain:
        for side in ("pe", "ce"):
            iv = float(row.get(f"{side}_iv") or 0)
            if iv <= 0:
                continue
            key = (row["strike"], side)
            if key not in _iv_history:
                _iv_history[key] = []
            _iv_history[key].append((now, iv))
            if len(_iv_history[key]) > 20:
                _iv_history[key] = _iv_history[key][-20:]


def iv_persistence_score(strike, side, window_minutes=15):
    """
    Returns 0-100 score where:
      > 65 = IV expanding (don't sell yet — let it peak)
      30-65 = IV stable
      < 30  = IV contracting (prime time to sell)
    Returns None if insufficient history.
    """
    key     = (strike, side.lower())
    history = _iv_history.get(key, [])
    if len(history) < 4:
        return None

    cutoff  = datetime.now(_IST) - timedelta(minutes=window_minutes)
    recent  = [(t, iv) for t, iv in history if t >= cutoff]
    if len(recent) < 3:
        return None

    ivs = [iv for _, iv in recent]
    # Simple linear slope: positive = expanding
    n   = len(ivs)
    xs  = list(range(n))
    mx  = sum(xs) / n
    my  = sum(ivs) / n
    cov = sum((xs[i] - mx) * (ivs[i] - my) for i in range(n))
    var = sum((x - mx) ** 2 for x in xs) or 1
    slope = cov / var   # IV points per bar

    # Normalise to 0-100: slope of +0.3 iv-pts/bar ≈ 100, -0.3 ≈ 0
    score = 50 + (slope / 0.3) * 50
    return round(max(0, min(100, score)), 1)


# ── Zone lookup ───────────────────────────────────────────────────────────────

def get_deep_otm_zone(dte):
    try:
        from config import DEEP_OTM_ZONE_BY_DTE
        return DEEP_OTM_ZONE_BY_DTE.get(max(1, min(7, dte)), (900, 1400))
    except Exception:
        return (900, 1400)


# ── Signal check ──────────────────────────────────────────────────────────────

def check_deep_otm_signal(chain, spot, dte, side,
                           spot_change_pct_1h=0.0, vix_now=0.0,
                           event_within_3d=False, now_dt=None):
    """
    Returns (signal_dict | None, reason_str).
    signal_dict has the best strike + entry recommendation.
    """
    try:
        from config import (
            DEEP_OTM_EVENT_BLOCK_DAYS,
            DEEP_OTM_HEDGE_REQUIRED,
            DEEP_OTM_MAX_LTP,
            DEEP_OTM_MAX_VIX,
            DEEP_OTM_MIN_IV_INFLATION,
            DEEP_OTM_MIN_LTP,
            DEEP_OTM_MIN_SIGNALS,
        )
    except Exception:
        DEEP_OTM_MIN_IV_INFLATION = 3.0
        DEEP_OTM_MIN_LTP          = 2.0
        DEEP_OTM_MAX_LTP          = 15.0
        DEEP_OTM_MIN_SIGNALS      = 2
        DEEP_OTM_MAX_VIX          = 20.0
        DEEP_OTM_EVENT_BLOCK_DAYS = 3
        DEEP_OTM_HEDGE_REQUIRED   = True

    # Safety guards
    if event_within_3d:
        return None, "event within 3 days — no deep OTM entry"
    if vix_now > DEEP_OTM_MAX_VIX:
        return None, f"VIX {vix_now:.1f} > {DEEP_OTM_MAX_VIX} — elevated fear, skip"

    # Friday afternoon: only if we explicitly allow (caller should check persistence)
    if now_dt is None:
        now_dt = datetime.now(_IST)
    if now_dt.weekday() == 4 and now_dt.hour >= 14:
        # Checked separately by caller via persistence score — flag it here
        pass

    # Direction safety
    if side == "PE" and spot_change_pct_1h < -0.3:
        return None, f"spot down {spot_change_pct_1h:.2f}% last hour — not safe for PE deep OTM"
    if side == "CE" and spot_change_pct_1h > 0.3:
        return None, f"spot up {spot_change_pct_1h:.2f}% last hour — not safe for CE deep OTM"

    # IV inflation
    inflations = compute_iv_inflation(chain, spot, side)
    strong     = [i for i in inflations if i["inflation_abs"] >= DEEP_OTM_MIN_IV_INFLATION]
    if len(strong) < DEEP_OTM_MIN_SIGNALS:
        return None, (f"only {len(strong)} strike(s) with IV inflation ≥{DEEP_OTM_MIN_IV_INFLATION}pp "
                      f"(need {DEEP_OTM_MIN_SIGNALS})")

    # Zone filter
    zone_min, zone_max = get_deep_otm_zone(dte)
    in_zone = [i for i in strong if zone_min <= i["distance"] <= zone_max]
    if not in_zone:
        return None, f"no IV-inflated strikes in {zone_min}–{zone_max}pt zone for DTE {dte}"

    # LTP filter
    tradeable = [i for i in in_zone
                 if DEEP_OTM_MIN_LTP <= i["ltp"] <= DEEP_OTM_MAX_LTP]
    if not tradeable:
        return None, (f"no strikes meet ₹{DEEP_OTM_MIN_LTP}–₹{DEEP_OTM_MAX_LTP} LTP range "
                      f"(best={in_zone[0]['ltp']:.2f})")

    best = tradeable[0]

    # IV persistence check — don't sell into still-rising IV
    persist = iv_persistence_score(best["strike"], side)
    if persist is not None and persist > 65:
        return None, (f"IV still expanding on {best['strike']} (persistence {persist:.0f}) — wait for peak")

    return best, "signal confirmed"


# ── Sizing ────────────────────────────────────────────────────────────────────

def deep_otm_size(signal):
    try:
        from config import DEEP_OTM_MAX_LOTS
    except Exception:
        DEEP_OTM_MAX_LOTS = 15
    inflation = signal.get("inflation_abs", 0)
    if inflation >= 5.0:
        size = 15
    elif inflation >= 4.0:
        size = 12
    else:
        size = 10
    return min(size, DEEP_OTM_MAX_LOTS)


# ── Exit check ────────────────────────────────────────────────────────────────

def check_deep_otm_exit(trade, spot, chain_map, time_str):
    """
    Returns exit_reason string or None (no exit yet).
    Called every refresh from _update_deep_otm_spread.
    """
    try:
        from config import DEEP_OTM_STOP_MULT, DEEP_OTM_TARGET_DECAY
    except Exception:
        DEEP_OTM_TARGET_DECAY = 0.70
        DEEP_OTM_STOP_MULT    = 3.0

    entry   = trade.get("net_credit_entry", 0)
    current = trade.get("net_credit_current", entry)
    if entry <= 0:
        return None

    # 70% decay target
    if current <= entry * (1 - DEEP_OTM_TARGET_DECAY):
        return "TARGET"

    # 3× stop
    if current >= entry * DEEP_OTM_STOP_MULT:
        return "STOP"

    # IV re-expansion warning (sell-strike IV expanding sharply)
    side    = trade.get("side", "PE").lower()
    sell_s  = trade.get("sell_strike")
    persist = iv_persistence_score(sell_s, side) if sell_s else None
    if persist is not None and persist > 75:
        return "IV_REEXPANSION"

    # Spot approaching short strike — 60% of buffer consumed
    entry_spot   = trade.get("entry_spot", spot)
    if entry_spot:
        d_entry = abs(entry_spot - (sell_s or entry_spot))
        d_now   = abs(spot - (sell_s or spot))
        if d_entry > 0 and d_now < d_entry * 0.4:
            return "SPOT_APPROACHING"

    return None


# ── Carry-forward evaluator ───────────────────────────────────────────────────

def evaluate_carry_forward(trade, spot, gap_stats, now_dt=None):
    """
    Called at ~15:15 IST to decide carry vs exit.
    Returns dict: {verdict, safety_multiple, reasoning}
    """
    if now_dt is None:
        now_dt = datetime.now(_IST)

    sell_strike = trade.get("sell_strike", 0)
    if not sell_strike or not spot:
        return {"verdict": "EXIT", "safety_multiple": 0, "reasoning": "missing data"}

    distance    = abs(spot - sell_strike)
    p90_gap_pct = float((gap_stats or {}).get("p90", 0.5))
    p90_pts     = spot * p90_gap_pct / 100
    safety_mult = round(distance / p90_pts, 2) if p90_pts > 0 else 0

    if safety_mult >= 3.0:
        verdict   = "CARRY_SAFE"
        reasoning = (f"strike {distance:.0f}pt away, p90 overnight gap ~{p90_pts:.0f}pt "
                     f"({p90_gap_pct:.2f}%) — {safety_mult:.1f}× safety")
    elif safety_mult >= 2.0:
        verdict   = "CARRY_ACCEPTABLE"
        reasoning = (f"strike {distance:.0f}pt away, {safety_mult:.1f}× p90 gap — marginal")
    else:
        verdict   = "EXIT"
        reasoning = (f"strike only {distance:.0f}pt away ({safety_mult:.1f}× p90 gap) — too close")

    return {"verdict": verdict, "safety_multiple": safety_mult, "reasoning": reasoning}


# ── Top inflations summary for API ───────────────────────────────────────────

def top_iv_inflations(chain, spot, side, n=5):
    inflations = compute_iv_inflation(chain, spot, side)
    return inflations[:n]

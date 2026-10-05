"""
risk_engine.py
Session scoring, regime scoring, scenario matrix, carry panel.
Session risk scoring: VIX, gap, event and PCR vetoes.
Hard vetoes in session_score() override EVERYTHING — do not weaken them.
"""
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from oi_dashboard.core.paths import ROOT

IST = timezone(timedelta(hours=5, minutes=30))

try:
    from config import (
        DIVIDEND_YIELD,
        EVENTS_JSON,
        GAP_VETO_PCT,
        RISK_FREE_RATE,
        STRUCTURAL_IV_PREMIUM_PP,
        VIX_FLOOR,
        VIX_VETO,
    )
except ImportError:
    GAP_VETO_PCT            = 0.8
    VIX_VETO                = 20.0
    VIX_FLOOR               = 10.0
    RISK_FREE_RATE          = 0.065
    DIVIDEND_YIELD          = 0.012
    STRUCTURAL_IV_PREMIUM_PP = 0.03
    EVENTS_JSON             = "events.json"


# ── Events helpers ─────────────────────────────────────────────────────────────

def _load_events_map() -> dict:
    """Load events.json → {date_str: event_dict}. Highest-priority action wins."""
    events_path = Path(str(ROOT)) / "data" / "events.json"
    if not events_path.exists():
        return {}
    try:
        with open(events_path, encoding="utf-8") as f:
            raw = json.load(f)
        _priority = {"SKIP": 3, "IC_ONLY": 2, "REDUCED": 1}
        ev_map = {}
        for ev in raw:
            d = ev.get("date", "")
            if not d:
                continue
            existing = ev_map.get(d)
            if existing is None or _priority.get(ev["action"], 0) > _priority.get(existing.get("action", ""), 0):
                ev_map[d] = ev
        return ev_map
    except Exception:
        return {}


def _event_within_days(days: int = 3) -> dict | None:
    """Return the next event within `days` calendar days, or None."""
    ev_map = _load_events_map()
    today  = datetime.now(IST).date()
    upcoming = []
    for date_str, ev in ev_map.items():
        try:
            d    = datetime.strptime(date_str, "%Y-%m-%d").date()
            diff = (d - today).days
            if 1 <= diff <= days:
                upcoming.append((diff, ev))
        except ValueError:
            continue
    if not upcoming:
        return None
    upcoming.sort(key=lambda x: x[0])
    diff, ev = upcoming[0]
    return {**ev, "days_away": diff}


# ── Session score ──────────────────────────────────────────────────────────────

def session_score(spot_data: dict, vix: float, pcr: float,
                  iv_premium_pct: float, dte: int,
                  sell_signals: list, gap_pct: float = 0.0,
                  now=None) -> dict:
    """
    Hard vetoes first (return score=0, trade=False immediately):
      - Event today with action=SKIP
      - |gap%| >= GAP_VETO_PCT
      - VIX >= VIX_VETO

    Then score 0-10 from 8 checks (1.25 pts each):
      1. VIX zone (8-14% = good; 14-18 = ok; >18 = bad)
      2. PCR (0.9-1.4 = good; extremes = bad)
      3. IV premium > 0 (market IV > RV blend)
      4. DTE 2-6 (not 0/1 expiry day, not too far)
      5. Weekday (Mon-Thu = ok; Fri = risk of weekend gap)
      6. Strong sell signals found
      7. Gap < 0.4% (tight open = premium decays well)
      8. No event within 2 days
    """
    if now is None:
        now = datetime.now(IST)
    today_str = now.strftime("%Y-%m-%d")
    weekday   = now.weekday()   # 0=Mon, 4=Fri

    # ── Hard vetoes ──────────────────────────────────────────────────────────
    ev_map     = _load_events_map()
    today_event = ev_map.get(today_str)
    if today_event and today_event.get("action") == "SKIP":
        return {
            "score": 0, "trade": False, "veto": True,
            "veto_reason": f"EVENT: {today_event.get('label', 'event')}",
            "checks": [], "today_event": today_event,
        }

    if abs(gap_pct) >= GAP_VETO_PCT:
        return {
            "score": 0, "trade": False, "veto": True,
            "veto_reason": f"GAP {gap_pct:+.2f}% >= ±{GAP_VETO_PCT}%",
            "checks": [],
        }

    if vix and vix >= VIX_VETO:
        return {
            "score": 0, "trade": False, "veto": True,
            "veto_reason": f"VIX {vix:.1f} >= {VIX_VETO} — too high",
            "checks": [],
        }

    if vix and 0 < vix < VIX_FLOOR:
        return {
            "score": 0, "trade": False, "veto": True,
            "veto_reason": f"VIX {vix:.1f} < {VIX_FLOOR} — premium too thin, not worth tail risk",
            "checks": [],
        }

    # ── Scoring ───────────────────────────────────────────────────────────────
    checks = []

    def _check(name, passed, note=""):
        checks.append({"name": name, "passed": passed, "note": note})
        return 1 if passed else 0

    # 1. VIX zone (floor already vetoed above; here just check upper band)
    vix_ok = VIX_FLOOR <= (vix or 0) <= 18
    _check("VIX zone", vix_ok,
           f"VIX={vix:.1f} ({'ok' if vix_ok else 'too high' if (vix or 0) > 18 else f'< {VIX_FLOOR} floor'})")

    # 2. PCR
    pcr_ok = 0.85 <= (pcr or 1.0) <= 1.5
    _check("PCR", pcr_ok, f"PCR={pcr:.2f}" if pcr else "PCR=n/a")

    # 3. IV premium
    iv_prem_ok = (iv_premium_pct or 0) > 0
    _check("IV premium > 0", iv_prem_ok,
           f"IV-RV={iv_premium_pct:+.1f}pp" if iv_premium_pct is not None else "n/a")

    # 4. DTE
    dte_ok = 2 <= (dte or 0) <= 6
    _check("DTE 2-6", dte_ok, f"DTE={dte}")

    # 5. Weekday
    day_ok = weekday <= 3   # Mon-Thu
    _check("Weekday Mon-Thu", day_ok,
           ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][weekday])

    # 6. Sell signals found
    n_signals = len([s for s in (sell_signals or []) if s])
    sigs_ok   = n_signals >= 1
    _check("Sell signals", sigs_ok, f"{n_signals} signal(s)")

    # 7. Gap tight
    gap_ok = abs(gap_pct) < 0.40
    _check("Gap < 0.4%", gap_ok, f"gap={gap_pct:+.2f}%")

    # 8. No event within 2 days
    upcoming = _event_within_days(2)
    ev_clear  = upcoming is None
    _check("No event ≤2d", ev_clear,
           "clear" if ev_clear else f"event in {upcoming['days_away']}d: {upcoming.get('label','?')}")

    score = round(sum(c["passed"] for c in checks) / len(checks) * 10, 1)
    return {
        "score":        score,
        "trade":        score >= 6.0,
        "veto":         False,
        "veto_reason":  None,
        "checks":       checks,
        "today_event":  today_event,
        "upcoming_event": upcoming,
    }


# ── Regime score ──────────────────────────────────────────────────────────────

def regime_score(vix: float, spot_rows: list, atm_iv: float,
                 sigma_blend: float, skew_rr25: float = None) -> dict:
    """
    Five factors scored -2 to +2, weighted sum normalized to 0-10.
    Factors: VIX z-score, RV trend, term structure, put skew, VRP.
    size_multiplier: 1.0 (score≥7), 0.75 (5-6), 0.5 (3-4), 0.0 (<3)
    """
    factors = []

    # ── Factor 1: VIX z-score (weight 35%) ────────────────────────────────────
    vix_z = None
    if vix and spot_rows and len(spot_rows) >= 20:
        # Use rolling ATM IV proxy: abs log-returns as vol proxy
        closes = [r["close"] for r in spot_rows[-21:] if r.get("close", 0) > 0]
        if len(closes) >= 20:
            log_rets = [abs(math.log(closes[i] / closes[i-1]))
                        for i in range(1, len(closes)) if closes[i-1] > 0]
            if log_rets:
                mean_lr = sum(log_rets) / len(log_rets)
                std_lr  = math.sqrt(sum((x - mean_lr)**2 for x in log_rets) / len(log_rets))
                # Approximate VIX z-score using current VIX vs recent vol
                vix_frac = (vix / 100.0)
                vix_z    = (vix_frac - mean_lr * math.sqrt(252)) / (std_lr * math.sqrt(252) + 1e-8)

    if vix_z is not None:
        if vix_z > 1.5:   f1 = 2
        elif vix_z > 0.5: f1 = 1
        elif vix_z > -0.5:f1 = 0
        elif vix_z > -1.5:f1 = -1
        else:              f1 = -2
    else:
        f1 = 0
    factors.append({"name": "VIX z-score", "raw": round(vix_z, 2) if vix_z else None,
                    "score": f1, "weight": 0.35})

    # ── Factor 2: RV trend rv5/rv20 (weight 25%) ──────────────────────────────
    try:
        from oi_dashboard.analytics.vol_engine import compute_all_rvs
        closes_all = [r["close"] for r in spot_rows if r.get("close", 0) > 0]
        rvs = compute_all_rvs(spot_rows, closes_all)
        rv5  = rvs.get("rv5")  or 0
        rv20 = rvs.get("rv20") or 0
        ratio = rv5 / rv20 if rv20 > 0 else 1.0
    except Exception:
        ratio = 1.0

    if ratio < 0.75:   f2 = 2
    elif ratio < 0.90: f2 = 1
    elif ratio <= 1.10:f2 = 0
    elif ratio <= 1.30:f2 = -1
    else:              f2 = -2
    factors.append({"name": "RV trend (rv5/rv20)", "raw": round(ratio, 2),
                    "score": f2, "weight": 0.25})

    # ── Factor 3: Term structure slope (weight 20%) — skip if no data ─────────
    # Requires near-week and far-week IV — unavailable without multi-expiry chain.
    # Skip for now; weight redistributed proportionally to factors 1+2.
    f3 = None
    factors.append({"name": "Term structure", "raw": None,
                    "score": None, "weight": 0.20,
                    "note": "unavailable — need multi-expiry IV"})

    # ── Factor 4: Put skew RR25 (weight 10%) ──────────────────────────────────
    if skew_rr25 is not None:
        if skew_rr25 > 4.0:   f4 = 2
        elif skew_rr25 > 2.5: f4 = 1
        elif skew_rr25 >= 1.0:f4 = 0
        else:                  f4 = -1
    else:
        f4 = 0
    factors.append({"name": "Put skew RR25", "raw": skew_rr25,
                    "score": f4, "weight": 0.10})

    # ── Factor 5: VRP = atm_iv - sigma_blend (weight 10%) ────────────────────
    vrp = (atm_iv - sigma_blend * 100) if (atm_iv and sigma_blend) else None
    if vrp is not None:
        if vrp > 3.0:   f5 = 2
        elif vrp > 1.5: f5 = 1
        elif vrp > 0:   f5 = 0
        else:           f5 = -1
    else:
        f5 = 0
    factors.append({"name": "VRP (IV-sigma)", "raw": round(vrp, 2) if vrp else None,
                    "score": f5, "weight": 0.10})

    # ── Weighted sum (skip None scores, redistribute weight) ─────────────────
    active = [(f["score"], f["weight"]) for f in factors if f["score"] is not None]
    if not active:
        raw_score = 5.0
    else:
        total_w   = sum(w for _, w in active)
        raw_score = sum(s * w for s, w in active) / total_w  # in range -2..+2
        # Normalize -2..+2 → 0..10
        raw_score = (raw_score + 2) / 4.0 * 10.0

    score = round(max(0.0, min(10.0, raw_score)), 1)

    if score >= 7.0:   size_mult = 1.0;  verdict = "STRONG"
    elif score >= 5.0: size_mult = 0.75; verdict = "MODERATE"
    elif score >= 3.0: size_mult = 0.5;  verdict = "WEAK"
    else:              size_mult = 0.0;  verdict = "AVOID"

    # Build hard vetoes list for dashboard display
    vetoes = []
    if vix and vix >= VIX_VETO:
        vetoes.append(f"VIX {vix:.1f} ≥ {VIX_VETO} — too high")
    if vix and 0 < vix < VIX_FLOOR:
        vetoes.append(f"VIX {vix:.1f} < {VIX_FLOOR} — premium too thin")

    return {
        "score":           score,
        "factors":         factors,
        "size_multiplier": size_mult,
        "verdict":         verdict,
        "vix_zscore":      round(vix_z, 2) if vix_z else None,
        "vrp":             round(vrp, 2) if vrp else None,
        "vetoes":          vetoes,
    }


# ── Scenario matrix ────────────────────────────────────────────────────────────

def scenario_matrix(positions: list, spot: float, chain: list, dte: int) -> dict:
    """
    P&L grid at spot ±0.5/1/2/3%.
    Down moves apply an IV bump (crash behavior — vega hurts sellers).
    """
    from oi_dashboard.analytics.vol_engine import bs_price

    try:
        from config import LOT_SIZE
    except ImportError:
        LOT_SIZE = 65

    if not positions or not chain:
        return {"scenarios": [], "note": "no positions"}

    chain_map = {r["strike"]: r for r in chain}
    moves_pct = [-3, -2, -1, -0.5, 0, 0.5, 1, 2, 3]
    scenarios = []

    for move_pct in moves_pct:
        new_spot  = spot * (1 + move_pct / 100)
        iv_bump   = max(0.0, -move_pct * 0.3)   # down moves add IV (crash skew)
        total_pnl = 0.0

        for pos in positions:
            strike  = pos.get("strike")
            opt     = pos.get("option", "CE")
            qty     = pos.get("qty", 1)
            ep      = pos.get("entry_price", 0)
            is_sell = pos.get("type") == "SELL"

            row     = chain_map.get(strike, {})
            iv_now  = float(row.get(f"{opt.lower()}_iv", 14)) / 100.0 + iv_bump / 100.0
            iv_now  = max(iv_now, 0.05)

            new_price = bs_price(new_spot, strike, iv_now, max(dte - 1, 0.02), opt)
            if is_sell:
                pnl_unit = ep - new_price
            else:
                pnl_unit = new_price - ep
            total_pnl += pnl_unit * qty * LOT_SIZE

        scenarios.append({
            "move_pct":  move_pct,
            "new_spot":  round(new_spot, 1),
            "pnl":       round(total_pnl, 0),
            "pnl_per_lot": round(total_pnl / max(sum(p.get("qty",1) for p in positions), 1), 0),
            "iv_bump":   round(iv_bump, 1),
        })

    return {"scenarios": scenarios}


# ── Carry panel ────────────────────────────────────────────────────────────────

def carry_panel(positions: list, spot: float, chain: list, expiry: str) -> dict:
    """
    Overnight carry decision.
    Evaluates: time decay earned, gap risk at p50/p90, net recommendation.
    """
    from oi_dashboard.analytics.vol_engine import bs_price, gap_stats

    try:
        from config import LOT_SIZE, SPOT_HISTORY_CSV
    except ImportError:
        LOT_SIZE         = 65
        SPOT_HISTORY_CSV = ""

    if not positions:
        return {"recommendation": "NO_POSITIONS", "details": []}

    chain_map = {r["strike"]: r for r in chain}

    # Load gap stats
    g_stats = {}
    if SPOT_HISTORY_CSV:
        try:
            import csv
            rows = []
            with open(SPOT_HISTORY_CSV, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    try:
                        rows.append({
                            "open":  float(row.get("open", 0) or 0),
                            "close": float(row.get("close", 0) or 0),
                        })
                    except Exception:
                        continue
            g_stats = gap_stats(rows, 90)
        except Exception:
            pass

    details = []
    theta_day = 0.0

    for pos in positions:
        strike  = pos.get("strike")
        opt     = pos.get("option", "CE")
        qty     = pos.get("qty", 1)
        ep      = pos.get("entry_price", 0)
        is_sell = pos.get("type") == "SELL"
        row     = chain_map.get(strike, {})
        iv      = float(row.get(f"{opt.lower()}_iv", 14)) / 100.0

        try:
            from oi_dashboard.analytics.greeks import compute_greeks
            g = compute_greeks(spot, strike, iv * 100, max(1, 1), opt)
            sign = -1 if is_sell else 1
            theta_pos = sign * g["theta"] * qty * LOT_SIZE  # Rs/day (seller gets +)
        except Exception:
            theta_pos = 0.0

        theta_day += theta_pos
        details.append({
            "strike": strike, "type": opt, "side": pos.get("type"),
            "theta_rs_day": round(theta_pos, 1),
        })

    gap_p50 = g_stats.get("p50", 0)
    gap_p90 = g_stats.get("p90", 0)

    # Simple recommendation
    if theta_day > 0 and gap_p90 < spot * 0.008:
        rec = "CARRY — theta dominates at p90 gap risk"
    elif theta_day > 0:
        rec = "BORDERLINE — theta positive but gap risk elevated"
    else:
        rec = "HEDGE — sellers losing theta benefit"

    return {
        "theta_per_day_rs": round(theta_day, 1),
        "gap_p50_pts":      round(gap_p50, 0),
        "gap_p90_pts":      round(gap_p90, 0),
        "recommendation":   rec,
        "details":          details,
    }

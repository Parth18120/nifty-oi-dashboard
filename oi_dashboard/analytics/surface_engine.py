"""
surface_engine.py
Implied volatility surface extraction, surface OVR%, skew history logging.

IV source priority:
  1. Dhan's ce_iv / pe_iv if > 1.0%  (already extracted by dhan_feed.py)
  2. Bisection implied_vol() when Dhan IV is 0 or suspiciously low

All surface metrics are additive — existing API keys are never modified.
"""
import math
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

try:
    from config import DIVIDEND_YIELD, RISK_FREE_RATE
except ImportError:
    RISK_FREE_RATE = 0.065
    DIVIDEND_YIELD = 0.012

STRIKE_STEP = 50   # Nifty options step size in points

# Prune counter — only prune DB every 100th log call
_log_call_count = 0
_skew_logged_today_date = ""   # tracks date of last skew-history log


# ── implied_vol: bisection IV solver ──────────────────────────────────────────

def implied_vol(ltp: float, spot: float, strike: float, dte: float,
                opt_type: str, r: float = None, q: float = None,
                lo: float = 1.0, hi: float = 150.0,
                iterations: int = 200, tol: float = 1e-5) -> float | None:
    """
    Bisection IV solver. Returns sigma% such that bs_price(sigma%) == ltp.
    Returns None if ltp <= intrinsic, result out of [1%, 150%], or no convergence.
    Uses bs_price() from vol_engine — does not re-implement BS.
    """
    from oi_dashboard.analytics.vol_engine import bs_price, fractional_dte

    if r is None: r = RISK_FREE_RATE
    if q is None: q = DIVIDEND_YIELD

    if ltp <= 0 or dte <= 0.02:
        return None

    # Intrinsic check
    if opt_type == "CE":
        intrinsic = max(0.0, spot - strike)
    else:
        intrinsic = max(0.0, strike - spot)
    if ltp <= intrinsic + 1e-6:
        return None

    # Convert lo/hi from % to decimal fractions for bs_price
    lo_f = lo / 100.0
    hi_f = hi / 100.0

    price_hi = bs_price(spot, strike, hi_f, dte, opt_type, r, q)
    if price_hi < ltp:
        return None   # ltp above max possible price at 150% vol

    price_lo = bs_price(spot, strike, lo_f, dte, opt_type, r, q)
    if price_lo > ltp:
        return None   # ltp below price even at 1% vol (deep ITM / no solution)

    mid_f = (lo_f + hi_f) / 2.0
    for _ in range(iterations):
        price_mid = bs_price(spot, strike, mid_f, dte, opt_type, r, q)
        if abs(price_mid - ltp) < tol:
            break
        if price_mid < ltp:
            lo_f = mid_f
        else:
            hi_f = mid_f
        mid_f = (lo_f + hi_f) / 2.0

    result_pct = round(mid_f * 100.0, 2)
    if result_pct < 1.0 or result_pct > 150.0:
        return None
    return result_pct


# ── find_delta_strike ─────────────────────────────────────────────────────────

def find_delta_strike(chain: list, spot: float, atm_iv: float,
                      dte: float, opt_type: str,
                      target_delta: float = 0.25) -> dict | None:
    """
    Find the chain row whose BS delta is closest to target_delta.
    For PE: target_delta is the absolute value (delta is negative for puts).
    Uses atm_iv as vol input (good approximation for finding 25-delta strike).
    """
    from oi_dashboard.analytics.greeks_engine import compute_greeks

    if not chain or not atm_iv or atm_iv <= 0:
        return None

    best_row  = None
    best_diff = float("inf")

    for row in chain:
        try:
            g = compute_greeks(spot, row["strike"], atm_iv, max(dte, 0.1),
                               opt_type, RISK_FREE_RATE, DIVIDEND_YIELD)
            delta = abs(g.get("delta", 0))
            diff  = abs(delta - target_delta)
            if diff < best_diff:
                best_diff = diff
                best_row  = row
        except Exception:
            continue

    return best_row


# ── surface_metrics ───────────────────────────────────────────────────────────

def surface_metrics(chain: list, spot: float, dte: float,
                    sigma_blend: float) -> dict:
    """
    Extract implied vol surface metrics from the live chain.
    See module docstring for full spec.
    """
    if not chain or spot <= 0:
        return _empty_surface(sigma_blend)

    atm_strike = round(spot / STRIKE_STEP) * STRIKE_STEP
    chain_map  = {r["strike"]: r for r in chain}

    # ── Step 1: extract IVs for every strike ─────────────────────────────────
    iv_by_strike = {}
    bisection_needed = 0

    for row in chain:
        s      = row["strike"]
        ce_ltp = float(row.get("ce_ltp") or 0)
        pe_ltp = float(row.get("pe_ltp") or 0)

        # CE IV: prefer Dhan, bisect if 0 or < 1%
        dhan_ce = float(row.get("ce_iv") or 0)
        if dhan_ce > 1.0:
            ce_iv = dhan_ce
        elif ce_ltp > 0.05:
            ce_iv = implied_vol(ce_ltp, spot, s, dte, "CE")
            if ce_iv:
                bisection_needed += 1
        else:
            ce_iv = None

        # PE IV: prefer Dhan, bisect if 0 or < 1%
        dhan_pe = float(row.get("pe_iv") or 0)
        if dhan_pe > 1.0:
            pe_iv = dhan_pe
        elif pe_ltp > 0.05:
            pe_iv = implied_vol(pe_ltp, spot, s, dte, "PE")
            if pe_iv:
                bisection_needed += 1
        else:
            pe_iv = None

        iv_by_strike[s] = {"ce_iv": ce_iv, "pe_iv": pe_iv}

    valid_count = sum(
        1 for v in iv_by_strike.values()
        if v.get("ce_iv") or v.get("pe_iv")
    )

    if bisection_needed:
        print(f"[SURFACE] bisection needed for {bisection_needed} IVs (Dhan IV was 0)")

    # ── Step 2: ATM IV ────────────────────────────────────────────────────────
    atm_data     = iv_by_strike.get(atm_strike, {})
    atm_ce_iv    = atm_data.get("ce_iv")
    atm_pe_iv    = atm_data.get("pe_iv")
    atm_iv       = None
    atm_fallback = False

    if atm_ce_iv and atm_pe_iv:
        atm_iv = round((atm_ce_iv + atm_pe_iv) / 2.0, 2)
    elif atm_ce_iv:
        atm_iv = atm_ce_iv
    elif atm_pe_iv:
        atm_iv = atm_pe_iv
    else:
        atm_iv       = round(sigma_blend * 100.0, 2)
        atm_fallback = True

    # ── Step 3: 25-delta strikes ──────────────────────────────────────────────
    d25_put_row  = find_delta_strike(chain, spot, atm_iv, dte, "PE", 0.25)
    d25_call_row = find_delta_strike(chain, spot, atm_iv, dte, "CE", 0.25)

    d25_put_strike  = d25_put_row["strike"]  if d25_put_row  else None
    d25_call_strike = d25_call_row["strike"] if d25_call_row else None

    d25_put_iv  = (iv_by_strike.get(d25_put_strike,  {}).get("pe_iv") or
                   float((d25_put_row  or {}).get("pe_iv") or 0) or None) if d25_put_strike  else None
    d25_call_iv = (iv_by_strike.get(d25_call_strike, {}).get("ce_iv") or
                   float((d25_call_row or {}).get("ce_iv") or 0) or None) if d25_call_strike else None

    # ── Step 4: skew metrics ──────────────────────────────────────────────────
    skew_rr25 = round(d25_put_iv - d25_call_iv, 2) if (d25_put_iv and d25_call_iv) else None

    skew_bf25 = (round((d25_put_iv + d25_call_iv) / 2.0 - atm_iv, 2)
                 if (d25_put_iv and d25_call_iv and atm_iv) else None)

    # Skew slope: (IV(ATM-200) - IV(ATM+200)) / 400
    slope_pe_strike = atm_strike - 200
    slope_ce_strike = atm_strike + 200
    slope_pe_iv = iv_by_strike.get(slope_pe_strike, {}).get("pe_iv")
    slope_ce_iv = iv_by_strike.get(slope_ce_strike, {}).get("ce_iv")
    skew_slope  = round((slope_pe_iv - slope_ce_iv) / 400.0, 4) if (slope_pe_iv and slope_ce_iv) else None

    # ── Step 5: wing IVs ──────────────────────────────────────────────────────
    put_wing_iv  = iv_by_strike.get(atm_strike - 300, {}).get("pe_iv")
    call_wing_iv = iv_by_strike.get(atm_strike + 300, {}).get("ce_iv")

    # ── Step 6: put skew label ────────────────────────────────────────────────
    rr = skew_rr25 or 0.0
    if rr >= 4.0:
        skew_label = "HIGH PUT SKEW — sell puts"
        skew_color = "green"
    elif rr >= 2.5:
        skew_label = "MODERATE PUT SKEW"
        skew_color = "yellow"
    elif rr >= 1.0:
        skew_label = "MILD PUT SKEW"
        skew_color = "white"
    elif rr >= 0:
        skew_label = "SKEW COMPRESSED — avoid puts"
        skew_color = "red"
    else:
        skew_label = "CALL SKEW — unusual"
        skew_color = "red"

    # ── Step 7: VRP ───────────────────────────────────────────────────────────
    vrp = round(atm_iv - sigma_blend * 100.0, 2) if atm_iv else None
    if vrp is None:
        vrp_label = "n/a"
    elif vrp > 3.0:
        vrp_label = f"+{vrp:.1f}pp RICH"
    elif vrp > 1.0:
        vrp_label = f"+{vrp:.1f}pp ELEVATED"
    else:
        vrp_label = f"{vrp:+.1f}pp FAIR/CHEAP"

    # ── surface_valid ─────────────────────────────────────────────────────────
    surface_valid = (
        not atm_fallback
        and valid_count >= 8
        and d25_put_strike is not None
        and d25_call_strike is not None
    )

    return {
        "atm_iv":          atm_iv,
        "atm_strike":      atm_strike,
        "skew_rr25":       skew_rr25,
        "skew_bf25":       skew_bf25,
        "skew_slope":      skew_slope,
        "put_skew_label":  skew_label,
        "put_skew_color":  skew_color,
        "call_wing_iv":    call_wing_iv,
        "put_wing_iv":     put_wing_iv,
        "vrp":             vrp,
        "vrp_label":       vrp_label,
        "surface_valid":   surface_valid,
        "iv_by_strike":    iv_by_strike,
        "d25_put_strike":  d25_put_strike,
        "d25_call_strike": d25_call_strike,
        "valid_iv_count":  valid_count,
        "bisection_count": bisection_needed,
        "atm_fallback":    atm_fallback,
    }


def _empty_surface(sigma_blend: float) -> dict:
    return {
        "atm_iv": round(sigma_blend * 100.0, 2) if sigma_blend else None,
        "atm_strike": None, "skew_rr25": None, "skew_bf25": None,
        "skew_slope": None, "put_skew_label": "NO DATA", "put_skew_color": "grey",
        "call_wing_iv": None, "put_wing_iv": None, "vrp": None, "vrp_label": "n/a",
        "surface_valid": False, "iv_by_strike": {}, "d25_put_strike": None,
        "d25_call_strike": None, "valid_iv_count": 0, "bisection_count": 0,
        "atm_fallback": True,
    }


# ── log_surface_snapshot ──────────────────────────────────────────────────────

def log_surface_snapshot(metrics: dict, spot: float, ts: str = None) -> None:
    """Write one row to surface_snapshots. Only logs if surface_valid=True."""
    global _log_call_count
    if not metrics.get("surface_valid"):
        return
    try:
        from oi_dashboard.storage import db
        if ts is None:
            ts = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
        date = ts[:10]
        db.log_surface_snapshot({
            "ts":           ts,
            "date":         date,
            "spot":         spot,
            "atm_iv":       metrics.get("atm_iv"),
            "skew_rr25":    metrics.get("skew_rr25"),
            "skew_bf25":    metrics.get("skew_bf25"),
            "skew_slope":   metrics.get("skew_slope"),
            "call_wing_iv": metrics.get("call_wing_iv"),
            "put_wing_iv":  metrics.get("put_wing_iv"),
        })
        _log_call_count += 1
        if _log_call_count % 100 == 0:
            db.prune_surface_snapshots(keep_days=30)
    except Exception as e:
        print(f"[SURFACE] log snapshot failed: {e}")


# ── surface_ovr ───────────────────────────────────────────────────────────────

_MONEYNESS_BUCKETS = [200, 300, 400, 500, 600, 700, 800]
_BUCKET_WIDTH      = 75   # ±75pt around each bucket center


def surface_ovr(strike_iv: float, atm_iv: float,
                moneyness_pts: int, opt_type: str) -> tuple:
    """
    Compare today's scaled IV to 60-day historical bucket distribution.
    Returns (ovr_pct, valid).  valid=False if < 10 historical rows.
    """
    if not strike_iv or not atm_iv or atm_iv <= 0:
        return 0.0, False

    scaled_today = strike_iv / atm_iv

    # Find nearest moneyness bucket
    abs_m  = abs(moneyness_pts)
    bucket = min(_MONEYNESS_BUCKETS, key=lambda b: abs(abs_m - b))
    if abs(abs_m - bucket) > _BUCKET_WIDTH:
        return 0.0, False   # moneyness doesn't fall in any bucket

    # Signed bucket for skew_history (positive=CE side, negative=PE side)
    signed_bucket = bucket if opt_type == "CE" else -bucket

    try:
        from oi_dashboard.storage import db
        rows = db.get_skew_history(signed_bucket, opt_type, days=60)
    except Exception:
        return 0.0, False

    if len(rows) < 10:
        return 0.0, False

    scaled_vals = sorted(r["scaled_iv"] for r in rows)
    n = len(scaled_vals)
    mid = n // 2
    median = (scaled_vals[mid] if n % 2 == 1
              else (scaled_vals[mid - 1] + scaled_vals[mid]) / 2.0)

    if median <= 0:
        return 0.0, False

    ovr_pct = round((scaled_today - median) / median * 100.0, 1)
    return ovr_pct, True


# ── log_skew_history ──────────────────────────────────────────────────────────

def log_skew_history(metrics: dict, spot: float, date: str = None) -> None:
    """
    Write one row per strike to skew_history. Call once per session at ~14:00.
    INSERT OR REPLACE handles same-day re-runs.
    """
    if not metrics.get("atm_iv") or not metrics.get("iv_by_strike"):
        return
    if date is None:
        date = datetime.now(IST).strftime("%Y-%m-%d")

    atm_strike = round(spot / STRIKE_STEP) * STRIKE_STEP
    atm_iv     = metrics["atm_iv"]

    try:
        from oi_dashboard.storage import db
        for strike, ivs in metrics["iv_by_strike"].items():
            moneyness = strike - atm_strike
            # CE side: positive moneyness
            if moneyness > 0 and ivs.get("ce_iv"):
                scaled = ivs["ce_iv"] / atm_iv
                db.log_skew_row(date, moneyness, "CE", scaled, atm_iv, spot)
            # PE side: negative moneyness
            if moneyness < 0 and ivs.get("pe_iv"):
                scaled = ivs["pe_iv"] / atm_iv
                db.log_skew_row(date, moneyness, "PE", scaled, atm_iv, spot)
    except Exception as e:
        print(f"[SURFACE] log_skew_history failed: {e}")

"""
vol_engine.py
Volatility estimation engine (standalone version used by the backtester).
All functions produce IDENTICAL output to the originals.
Do NOT change yang_zhang() formula, _get_best_sigma() blend logic,
or _fractional_dte() values — these were deliberately tuned.
"""
import math
from datetime import datetime, timedelta, timezone
from datetime import time as _time
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))

try:
    from config import STRUCTURAL_IV_PREMIUM_PP
except ImportError:
    STRUCTURAL_IV_PREMIUM_PP = 0.03

try:
    from config import DIVIDEND_YIELD, RISK_FREE_RATE
except ImportError:
    RISK_FREE_RATE  = 0.065
    DIVIDEND_YIELD  = 0.012

try:
    from config import FAIR_FLOOR
except ImportError:
    FAIR_FLOOR = 0.05

try:
    from config import OVR_CAP
except ImportError:
    OVR_CAP = 999.0

# ── Shared state (mirrors the realized_vol module-level vars) ─────────────────────────
_spot_history       = []
_spot_history_dates = []
_ohlc_history       = []
_vol_audit_printed  = False
_rv_cache           = {}
_rv20_cache         = {}
_last_rv_date       = None
_last_vix           = 12.0   # updated by the server each refresh


def set_vix(vix: float):
    """Called by the server each refresh so _get_best_sigma() has current VIX."""
    global _last_vix
    if vix and vix > 0:
        _last_vix = vix


# ── Yang-Zhang OHLC volatility ─────────────────────────────────────────────────

def yang_zhang(rows, window=20):
    """
    Yang-Zhang OHLC volatility estimator (annualized fraction, e.g. 0.1132).
    Captures both overnight gaps (close→open) and intraday range (RS term).
    Requires list of {open,high,low,close} dicts in chronological order.
    k constant = 0.34/(1.34+(n+1)/(n-1)) — do not change.
    """
    if len(rows) < window + 1:
        return None
    data = rows[-(window + 1):]
    n = window

    overnight, daytime, rs_terms = [], [], []
    for i in range(1, n + 1):
        pc = data[i-1]["close"]; o = data[i]["open"]
        h  = data[i]["high"];    l = data[i]["low"]; c = data[i]["close"]
        if pc > 0 and o > 0:
            overnight.append(math.log(o / pc))
        if o > 0 and c > 0:
            daytime.append(math.log(c / o))
        if o > 0 and h > 0 and l > 0 and c > 0:
            rs_terms.append(math.log(h / c) * math.log(h / o) +
                            math.log(l / c) * math.log(l / o))

    if len(overnight) < 2 or len(daytime) < 2 or len(rs_terms) < 2:
        return None

    def _var(s):
        mu = sum(s) / len(s)
        return sum((x - mu) ** 2 for x in s) / (len(s) - 1)

    k = 0.34 / (1.34 + (n + 1) / (n - 1))
    yz_var = _var(overnight) + k * _var(daytime) + (1 - k) * (sum(rs_terms) / len(rs_terms))
    return round(math.sqrt(yz_var * 252), 4) if yz_var > 0 else None


def rv_close(rows, window: int):
    """Close-to-close realized vol, annualized."""
    closes = [r["close"] for r in rows if r.get("close", 0) > 0]
    if len(closes) < window + 1:
        return None
    prices   = closes[-(window + 1):]
    log_rets = [math.log(prices[i] / prices[i-1])
                for i in range(1, len(prices))
                if prices[i-1] > 0 and prices[i] > 0]
    if len(log_rets) < 2:
        return None
    mean_r   = sum(log_rets) / len(log_rets)
    variance = sum((r - mean_r)**2 for r in log_rets) / (len(log_rets) - 1)
    return round(math.sqrt(variance * 252), 4)


def ewma_vol(rows, lam: float = 0.94) -> float | None:
    """EWMA volatility (RiskMetrics). lam=0.94 standard."""
    closes = [r["close"] for r in rows if r.get("close", 0) > 0]
    if len(closes) < 3:
        return None
    log_rets = [math.log(closes[i] / closes[i-1])
                for i in range(1, len(closes))
                if closes[i-1] > 0 and closes[i] > 0]
    if not log_rets:
        return None
    var = log_rets[0] ** 2
    for r in log_rets[1:]:
        var = lam * var + (1 - lam) * r ** 2
    return round(math.sqrt(var * 252), 4)


def compute_all_rvs(ohlc_rows: list, closes: list) -> dict:
    """
    Compute RV5/10/20/60 + YZ20 + EWMA from provided rows.
    Returns dict with keys: rv5, rv10, rv20, rv60, yz20, ewma.
    rv60 kept for display; not used in sigma blend (replaced by rv20).
    """
    def _rv_window(prices, n):
        if len(prices) < n + 1:
            return None
        p = prices[-(n + 1):]
        log_rets = [math.log(p[i] / p[i-1]) for i in range(1, len(p))
                    if p[i-1] > 0 and p[i] > 0]
        if len(log_rets) < 2:
            return None
        mean_r   = sum(log_rets) / len(log_rets)
        variance = sum((r - mean_r)**2 for r in log_rets) / (len(log_rets) - 1)
        return round(math.sqrt(variance * 252), 4)

    return {
        "rv5":  _rv_window(closes, 5),
        "rv10": _rv_window(closes, 10),
        "rv20": _rv_window(closes, 20),
        "rv60": _rv_window(closes, 60),
        "yz20": yang_zhang(ohlc_rows, window=20) if len(ohlc_rows) >= 21 else None,
        "ewma": ewma_vol(ohlc_rows),
    }


def sigma_forecast(rvs: dict, vix: float = None) -> float:
    """
    Blended sigma — identical logic to analytics.realized_vol.get_best_sigma().
    Blend: (ewma×0.40 + yz20×0.30 + rv20×0.30) / 1.00
    RV20 replaced RV60 as anchor — backtested lower RMSE (4.11 vs 4.39) on 124-day NIFTY data.
    VIX floor: max(sigma, VIX×0.80). Hard min: 8%.
    Event spike filter: if RV5 > 1.5×RV20, use YZ20/RV20 single-selector instead.
    """
    rv5  = rvs.get("rv5")  or 0.0
    rv20 = rvs.get("rv20") or 0.0
    yz20 = rvs.get("yz20") or 0.0
    ewma = rvs.get("ewma") or 0.0
    _vix = vix if (vix and vix > 0) else _last_vix

    event_spike = (rv5 > 0 and rv20 > 0 and rv5 > rv20 * 1.5)
    if event_spike:
        print(f"[SIGMA] Event spike: RV5={rv5*100:.1f}% > 1.5×RV20={rv20*100:.1f}% — using YZ20/RV20")
        sigma = yz20 if yz20 >= 0.06 else rv20
    else:
        # Weighted blend; weights drop gracefully when a component is unavailable
        ew = 0.40 if ewma > 0 else 0.0
        yw = 0.30 if yz20 > 0 else 0.0
        rw = 0.30 if rv20 > 0 else 0.0
        total_w = ew + yw + rw
        if total_w > 0:
            sigma = (ewma * ew + yz20 * yw + rv20 * rw) / total_w
        else:
            sigma = max((_vix or 11.8) / 100.0 * 0.85, 0.08)
            print(f"[SIGMA] All RV windows low — VIX fallback: {sigma*100:.1f}%")

    if _vix and _vix > 0:
        vix_floor = (_vix / 100.0) * 0.80
        if sigma < vix_floor:
            sigma = vix_floor

    sigma = max(sigma, 0.08)
    if sigma > 0.30:
        print(f"[WARN] sigma {sigma*100:.1f}% unusually high — check OHLC data quality")
    return round(sigma, 4)


# ── Black-Scholes pricing ──────────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    try:
        from scipy.stats import norm
        return norm.cdf(x)
    except ImportError:
        pass
    # Abramowitz & Stegun approximation
    if x < -8.0: return 0.0
    if x > 8.0:  return 1.0
    if x < 0:    return 1.0 - _norm_cdf(-x)
    k  = 1.0 / (1.0 + 0.2316419 * x)
    k2 = k * k
    p  = (0.319381530 * k - 0.356563782 * k2
          + 1.781477937 * k2 * k - 1.821255978 * k2 * k2
          + 1.330274429 * k2 * k2 * k)
    pdf = math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)
    return 1.0 - pdf * p


def fractional_dte(dte_days: float) -> float:
    """
    Session-aware fractional DTE — identical to analytics.pricing._fractional_dte().
    Pre-market  → 1.0 (full day ahead)
    In-session  → linear decay over 375-min session (min 1 min remaining)
    Post-market → 0.001
    These values were deliberately tuned — do NOT change.
    """
    if dte_days > 0:
        return float(dte_days)

    _OPEN  = _time(9, 15)
    _CLOSE = _time(15, 30)
    _TOTAL = 375

    now_ist = datetime.now(IST)
    t = now_ist.time()

    if t < _OPEN:
        return 1.0
    if t >= _CLOSE:
        return 0.001

    elapsed   = (now_ist.hour * 60 + now_ist.minute) - (9 * 60 + 15)
    remaining = max(_TOTAL - elapsed, 1)
    return remaining / _TOTAL


def bs_price(spot: float, strike: float, sigma_pct: float,
             dte_days: float, opt_type: str,
             r: float = None, q: float = None) -> float:
    """
    Black-Scholes price.
    sigma_pct: annualized vol as a fraction (e.g. 0.132, not 13.2).
    dte_days: calendar days to expiry (passes through fractional_dte).
    Returns option price in rupees.
    """
    if r is None: r = RISK_FREE_RATE
    if q is None: q = DIVIDEND_YIELD

    T = fractional_dte(dte_days) / 365.0
    if T <= 0 or sigma_pct <= 0.01:
        # Return intrinsic value
        if opt_type == "CE":
            return max(spot - strike, 0.0)
        return max(strike - spot, 0.0)

    sigma = sigma_pct
    S, K  = spot, strike
    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma**2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    eq_T = math.exp(-q * T)
    er_T = math.exp(-r * T)

    if opt_type == "CE":
        return max(S * eq_T * _norm_cdf(d1) - K * er_T * _norm_cdf(d2), 0.0)
    return max(K * er_T * _norm_cdf(-d2) - S * eq_T * _norm_cdf(-d1), 0.0)


def strike_iv_premium(strike_iv: float, sigma: float) -> tuple:
    """
    How much is the market paying above our vol forecast for this specific strike?

    strike_iv: Dhan's implied vol for this strike (%)
    sigma:     blended vol forecast (%)

    Returns (iv_premium_pp, valid)
    iv_premium_pp: pp above sigma; positive = overpriced = sell candidate
    valid: False if strike_iv <= 0 or sigma <= 0
    """
    if strike_iv <= 0 or sigma <= 0:
        return 0.0, False
    return round(strike_iv - sigma, 2), True


def fair_and_ovr(ltp: float, spot: float, strike: float,
                 sigma_pct: float, dte_days: float, opt_type: str) -> tuple:
    """
    Compute (fair_price, ovr_pct, valid).
    Adds STRUCTURAL_IV_PREMIUM_PP to sigma before pricing (realistic fair).
    valid=False if fair < FAIR_FLOOR.
    ovr_pct clamped to OVR_CAP.
    """
    effective_sigma = sigma_pct + STRUCTURAL_IV_PREMIUM_PP
    fair = bs_price(spot, strike, effective_sigma, dte_days, opt_type)
    if fair < FAIR_FLOOR:
        return fair, None, False
    ovr = min((ltp - fair) / fair * 100.0, OVR_CAP)
    return round(fair, 2), round(ovr, 1), True


def gap_stats(rows: list, window: int = 90) -> dict:
    """
    Compute overnight gap percentile stats from OHLC rows.
    Returns {p50, p75, p90, p95, worst, mean} in points.
    """
    if len(rows) < 2:
        return {}
    gaps = []
    for i in range(1, len(rows)):
        prev_close = rows[i-1].get("close", 0)
        today_open = rows[i].get("open", 0)
        if prev_close > 0 and today_open > 0:
            gaps.append(abs(today_open - prev_close))
    if not gaps:
        return {}
    gaps_w = sorted(gaps[-window:])
    n = len(gaps_w)

    def _pct(p):
        idx = p / 100.0 * (n - 1)
        lo, hi = int(idx), min(int(idx) + 1, n - 1)
        return gaps_w[lo] + (idx - lo) * (gaps_w[hi] - gaps_w[lo])

    return {
        "p50":   round(_pct(50), 1),
        "p75":   round(_pct(75), 1),
        "p90":   round(_pct(90), 1),
        "p95":   round(_pct(95), 1),
        "worst": round(gaps_w[-1], 1),
        "mean":  round(sum(gaps_w) / n, 1),
        "n":     n,
    }


def vrp_calibration(days: int = 90) -> dict:
    """
    Compute historical ratio of ATM_IV / sigma from iv_snapshots (or surface_snapshots).

    For each day with both an IV snapshot AND enough spot history to compute sigma:
      ratio = atm_iv_pct / (sigma * 100)

    Returns stats dict; falls back to bootstrap estimate when < 10 days available.
    Bootstrap mean 1.185 from 4 known data points (ATM IV / RV blend).
    """
    _BOOTSTRAP_MEAN = 1.185

    def _label(r):
        if r is None:
            return 'UNKNOWN'
        if r > 1.25:
            return 'RICH — strong sell signal'
        if r >= 1.10:
            return 'ELEVATED — good signal'
        if r >= 0.95:
            return 'FAIR — thin edge'
        return 'CHEAP — do not sell'

    try:
        from oi_dashboard.storage import db as _db
        iv_rows   = _db.get_iv_snapshots(days)
        spot_rows = sorted(_db.get_spot_daily(days=days + 40), key=lambda r: r['date'])

        ratios       = []
        dated_ratios = []

        for iv_row in iv_rows:
            date_str = iv_row['date']
            atm_iv   = float(iv_row.get('atm_iv') or 0)
            if atm_iv <= 0:
                continue
            rows_upto = [r for r in spot_rows if r['date'] <= date_str]
            if len(rows_upto) < 21:
                continue
            closes = [r['close'] for r in rows_upto if r.get('close', 0) > 0]
            rvs    = compute_all_rvs(rows_upto, closes)
            sig    = sigma_forecast(rvs)
            if sig <= 0:
                continue
            ratio = atm_iv / (sig * 100.0)
            ratios.append(ratio)
            dated_ratios.append((date_str, ratio))

        n = len(ratios)
        if n < 10:
            return {'insufficient_data': True, 'n_days': n, 'vrp_ratio_mean': _BOOTSTRAP_MEAN}

        mean_r   = sum(ratios) / n
        variance = sum((r - mean_r) ** 2 for r in ratios) / (n - 1)
        std_r    = math.sqrt(variance)
        sorted_r = sorted(ratios)
        p25      = sorted_r[max(int(n * 0.25) - 1, 0)]
        p75      = sorted_r[min(int(n * 0.75), n - 1)]
        mid      = n // 2
        median_r = sorted_r[mid] if n % 2 else (sorted_r[mid - 1] + sorted_r[mid]) / 2.0

        # Current: latest IV snapshot vs sigma computed from full available spot history
        all_closes  = [r['close'] for r in spot_rows if r.get('close', 0) > 0]
        curr_rvs    = compute_all_rvs(spot_rows, all_closes)
        curr_sig    = sigma_forecast(curr_rvs)
        latest_atm  = float(iv_rows[-1].get('atm_iv') or 0) if iv_rows else 0.0
        curr_ratio  = (latest_atm / (curr_sig * 100.0)) if (curr_sig > 0 and latest_atm > 0) else None
        curr_pct    = round(sum(1 for r in sorted_r if r <= curr_ratio) / n * 100, 1) \
                      if curr_ratio is not None else None

        # ATM IV forecast: sigma * median ratio
        vrp_ratio_for_forecast = median_r
        atm_iv_forecast = round(curr_sig * 100.0 * vrp_ratio_for_forecast, 2)

        return {
            'vrp_ratio_mean':      round(mean_r,   3),
            'vrp_ratio_median':    round(median_r, 3),
            'vrp_ratio_std':       round(std_r,    3),
            'vrp_ratio_p25':       round(p25,      3),
            'vrp_ratio_p75':       round(p75,      3),
            'n_days':              n,
            'current_ratio':       round(curr_ratio, 3) if curr_ratio is not None else None,
            'current_label':       _label(curr_ratio),
            'current_percentile':  curr_pct,
            'atm_iv_forecast':     atm_iv_forecast,
        }

    except Exception as e:
        return {'insufficient_data': True, 'n_days': 0, 'vrp_ratio_mean': _BOOTSTRAP_MEAN,
                'error': str(e)}


def forecast_strike_iv(atm_iv_forecast: float,
                       moneyness_pts: int,
                       opt_type: str) -> tuple:
    """
    Forecast a specific strike's IV using historical skew multipliers from skew_history.

    moneyness_pts: absolute OTM distance in points (always positive).
    opt_type: 'CE' or 'PE'.

    Snaps to nearest 100-pt bucket. Returns (forecast_iv, reliable).
    reliable=False when fewer than 10 historical observations exist for that bucket.

    Bootstrap fallbacks (from 2 known live sessions):
      CE: +100→0.975, +200→1.034, +300→1.101, +400→1.198
      PE: -100→1.066, -200→1.082, -300→1.145, -400→1.258
    For buckets > 400 without data, linearly extrapolates the slope.
    """
    BUCKETS = [100, 200, 300, 400, 500, 600, 700, 800]
    BOOTSTRAP_CE = {100: 0.975, 200: 1.034, 300: 1.101, 400: 1.198}
    BOOTSTRAP_PE = {100: 1.066, 200: 1.082, 300: 1.145, 400: 1.258}

    abs_pts = abs(moneyness_pts)
    bucket  = min(BUCKETS, key=lambda b: abs(b - abs_pts))
    # skew_history stores: positive moneyness → CE OTM; negative moneyness → PE OTM
    db_moneyness = bucket if opt_type == 'CE' else -bucket

    rows    = []
    reliable = False
    try:
        from oi_dashboard.storage import db as _db
        rows = _db.get_skew_history(db_moneyness, opt_type, days=90)
        reliable = len(rows) >= 10
    except Exception:
        pass

    if len(rows) >= 2:
        # scaled_iv already stores the ratio (strike_iv / atm_iv) from surface_engine
        ratios = [float(r['scaled_iv']) for r in rows if float(r.get('scaled_iv') or 0) > 0]
        if len(ratios) >= 2:
            ratios.sort()
            mid = len(ratios) // 2
            multiplier = ratios[mid] if len(ratios) % 2 else (ratios[mid - 1] + ratios[mid]) / 2.0
            return round(atm_iv_forecast * multiplier, 2), reliable

    # Bootstrap fallback
    bootstrap = BOOTSTRAP_CE if opt_type == 'CE' else BOOTSTRAP_PE
    if bucket in bootstrap:
        mult = bootstrap[bucket]
    else:
        # Extrapolate: slope from last two known points
        keys = sorted(bootstrap.keys())
        slope = (bootstrap[keys[-1]] - bootstrap[keys[-2]]) / 100.0
        mult  = bootstrap[keys[-1]] + slope * (bucket - keys[-1])
    return round(atm_iv_forecast * mult, 2), False


def overnight_intraday_split(rows: list) -> dict:
    """
    Split total vol into overnight gap component and intraday range component.
    Returns {overnight_pct, intraday_pct, total_pct} as annualized fractions.
    """
    if len(rows) < 2:
        return {}
    ov_sq, id_sq = [], []
    for i in range(1, len(rows)):
        pc = rows[i-1].get("close", 0)
        o  = rows[i].get("open", 0)
        h  = rows[i].get("high", 0)
        l  = rows[i].get("low", 0)
        c  = rows[i].get("close", 0)
        if pc > 0 and o > 0:
            ov_sq.append(math.log(o / pc) ** 2)
        if o > 0 and h > l > 0:
            hl_range = math.log(h / l)
            id_sq.append((hl_range / (2 * math.sqrt(math.log(2)))) ** 2)
    if not ov_sq or not id_sq:
        return {}
    ov_ann  = math.sqrt(sum(ov_sq) / len(ov_sq) * 252)
    id_ann  = math.sqrt(sum(id_sq) / len(id_sq) * 252)
    tot_ann = math.sqrt(ov_ann**2 + id_ann**2)
    return {
        "overnight_pct": round(ov_ann, 4),
        "intraday_pct":  round(id_ann, 4),
        "total_pct":     round(tot_ann, 4),
    }

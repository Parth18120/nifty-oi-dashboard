"""
greeks.py
Black-Scholes option Greeks calculation.
Used to show Delta, Gamma, Theta, Vega for any strike.
"""

import math

# Try scipy first; fall back to pure-math approximation
try:
    from scipy.stats import norm as _norm
    def _cdf(x):
        return _norm.cdf(x)
    def _pdf(x):
        return _norm.pdf(x)
except ImportError:
    # Abramowitz & Stegun approximation for standard normal CDF
    def _cdf(x):
        if x < -8.0:
            return 0.0
        if x > 8.0:
            return 1.0
        if x < 0:
            return 1.0 - _cdf(-x)
        k = 1.0 / (1.0 + 0.2316419 * x)
        k2 = k * k
        p = (0.319381530 * k
             - 0.356563782 * k2
             + 1.781477937 * k2 * k
             - 1.821255978 * k2 * k2
             + 1.330274429 * k2 * k2 * k)
        return 1.0 - _pdf(x) * p

    def _pdf(x):
        return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def compute_greeks(spot, strike, iv, days_to_expiry,
                   option_type="CE",
                   risk_free=0.065,
                   dividend=0.012):
    """
    Compute all Greeks for one option using Black-Scholes.

    iv is passed as a percentage (e.g. 13.5 means 13.5%).
    This function divides by 100 internally.

    Theta returned as per-day decay.
    Vega returned as change per 1% change in IV.
    """
    if iv <= 0 or days_to_expiry <= 0:
        return {"delta": 0, "gamma": 0, "theta": 0,
                "vega": 0, "price": 0, "error": "Invalid inputs"}

    T     = days_to_expiry / 365.0
    r     = risk_free
    q     = dividend
    S     = spot
    K     = strike
    sigma = iv / 100.0

    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T

    eq_T  = math.exp(-q * T)
    er_T  = math.exp(-r * T)

    if option_type == "CE":
        price = S * eq_T * _cdf(d1) - K * er_T * _cdf(d2)
        delta = eq_T * _cdf(d1)
        cdf_sign = _cdf(d2)
        cdf_s    = _cdf(d1)
    else:
        price = K * er_T * _cdf(-d2) - S * eq_T * _cdf(-d1)
        delta = -eq_T * _cdf(-d1)
        cdf_sign = _cdf(-d2)
        cdf_s    = _cdf(-d1)

    gamma = (eq_T * _pdf(d1)) / (S * sigma * sqrt_T)

    # Theta per calendar day
    if option_type == "CE":
        theta_num = (-(S * eq_T * _pdf(d1) * sigma) / (2 * sqrt_T)
                     - r * K * er_T * _cdf(d2)
                     + q * S * eq_T * _cdf(d1))
    else:
        theta_num = (-(S * eq_T * _pdf(d1) * sigma) / (2 * sqrt_T)
                     + r * K * er_T * _cdf(-d2)
                     - q * S * eq_T * _cdf(-d1))
    theta = theta_num / 365.0

    # Vega per 1% change in IV
    vega = S * eq_T * _pdf(d1) * sqrt_T / 100.0

    return {
        "delta": round(delta, 4),
        "gamma": round(gamma, 6),
        "theta": round(theta, 4),   # per day -- positive means option decays
        "vega":  round(vega, 4),    # per 1% IV change
        "price": round(price, 2),
        "error": None,
    }


def portfolio_greeks(open_trades: list, spot: float, chain: list,
                     days_to_expiry: float) -> dict | None:
    """
    Compute book-level Greeks for all OPEN paper trades.

    open_trades: list of paper trade dicts (from _paper_trades filtered to status=OPEN).
    Returns dict with net_delta_rs_per_pt, net_theta_rs_day, net_vega_rs_1pct,
    plus the full compute_position_greeks output, or None if no trades.

    All paper trades are SHORT positions (sellers).
    """
    if not open_trades:
        return None

    try:
        from config import LOT_SIZE as _LOT_SIZE
    except ImportError:
        _LOT_SIZE = 65

    positions = []
    for t in open_trades:
        strike = t.get("strike")
        opt_type = t.get("opt_type") or t.get("option")
        lots = t.get("lots", 1)
        entry_ltp = t.get("entry_ltp") or t.get("entry_price") or 0.0
        if not strike or not opt_type:
            continue
        positions.append({
            "strike":      int(strike),
            "option":      str(opt_type).upper(),
            "qty":         int(lots),
            "entry_price": float(entry_ltp),
            "type":        "SELL",
        })

    if not positions:
        return None

    pg = compute_position_greeks(positions, spot, chain, days_to_expiry)
    pg["net_delta_rs_per_pt"] = round(pg["net_delta"] * _LOT_SIZE, 2)
    # compute_position_greeks theta convention is inverted relative to what sellers expect:
    # a short position's theta comes out negative; negate to give seller a positive value.
    pg["net_theta_rs_day"]    = round(-pg["net_theta"], 2)
    pg["net_vega_rs_1pct"]    = round(pg["net_vega"],  2)
    return pg


def compute_position_greeks(positions, spot, chain, days_to_expiry):
    """
    Compute combined Greeks for all positions in MY_POSITIONS.
    Returns combined net Greeks and per-position breakdown.
    Lot size for Nifty = 65 (NSE revised Jan 2026; was 75).
    """
    try:
        from config import LOT_SIZE as _LOT_SIZE
    except ImportError:
        _LOT_SIZE = 65
    chain_map = {r["strike"]: r for r in chain}
    breakdown = []
    net_delta = 0.0
    net_gamma = 0.0
    net_theta = 0.0
    net_vega  = 0.0

    LOT_SIZE = _LOT_SIZE

    for pos in positions:
        strike      = pos["strike"]
        option_type = pos["option"]
        qty         = pos["qty"]
        entry_price = pos["entry_price"]
        is_sell     = pos["type"] == "SELL"

        row = chain_map.get(strike, {})
        iv  = row.get(f"{option_type.lower()}_iv", 0)
        ltp = row.get(f"{option_type.lower()}_ltp", 0)

        if iv <= 0:
            iv = 14.0  # fallback if IV not available

        g    = compute_greeks(spot, strike, iv, days_to_expiry, option_type)
        sign = -1 if is_sell else 1

        if is_sell:
            unrealised_pnl = (entry_price - ltp) * qty * LOT_SIZE
        else:
            unrealised_pnl = (ltp - entry_price) * qty * LOT_SIZE

        pos_delta = sign * g["delta"] * qty
        pos_gamma = sign * g["gamma"] * qty
        # Theta: seller collects decay (+), buyer pays decay (-)
        pos_theta = -sign * g["theta"] * qty * LOT_SIZE
        pos_vega  = sign * g["vega"] * qty * LOT_SIZE

        breakdown.append({
            "type":           pos["type"],
            "option":         option_type,
            "strike":         strike,
            "entry_price":    entry_price,
            "current_ltp":    ltp,
            "iv":             iv,
            "delta":          round(pos_delta, 4),
            "gamma":          round(pos_gamma, 6),
            "theta":          round(pos_theta, 2),  # Rs per day
            "vega":           round(pos_vega, 2),   # Rs per 1% IV
            "unrealised_pnl": round(unrealised_pnl, 2),
        })

        net_delta += pos_delta
        net_gamma += pos_gamma
        net_theta += pos_theta
        net_vega  += pos_vega

    return {
        "breakdown": breakdown,
        "net_delta": round(net_delta, 4),
        "net_gamma": round(net_gamma, 6),
        "net_theta": round(net_theta, 2),  # total Rs earned per day
        "net_vega":  round(net_vega, 2),   # total Rs change per 1% IV move
    }

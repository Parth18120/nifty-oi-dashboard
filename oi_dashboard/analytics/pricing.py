"""Black-Scholes fair-value helpers and option over-pricing percentage."""

import math
from datetime import datetime

from oi_dashboard.core.constants import IST


def bs_fair_with_vix(spot, strike, vix_pct, dte_days, option_type):
    """
    Black-Scholes fair price using VIX as sigma.
    Comparing option LTP to VIX-based fair value shows if the option is
    overpriced even relative to what the market expects.
    """
    if not vix_pct or vix_pct <= 0:
        return None, None
    sigma = vix_pct / 100.0
    return bs_fair_price(spot, strike, sigma, dte_days, option_type)


def _fractional_dte(dte_days):
    """
    Session-aware fractional DTE for Black-Scholes T.
    Non-expiry day (dte > 0): return calendar days unchanged.
    Expiry day pre-market (< 09:15 IST): return 1.0 (full session ahead).
    Expiry day in-session (09:15-15:30): decay linearly over 375-min session.
    Expiry day post-market (>= 15:30): return 0.001 (effectively expired).
    Avoids wall-clock undercount pre-market that collapses far-OTM fair values.
    """
    if dte_days > 0:
        return float(dte_days)
    # 0DTE: session-aware
    from datetime import time as _time
    now    = datetime.now(IST)
    t      = now.time()
    _OPEN  = _time(9, 15)
    _CLOSE = _time(15, 30)
    _TOTAL = 375   # 9:15 → 15:30 in minutes
    if t < _OPEN:
        return 1.0          # pre-market: entire session still ahead
    if t >= _CLOSE:
        return 0.001        # post-market: effectively expired
    elapsed   = (now.hour * 60 + now.minute) - (9 * 60 + 15)
    remaining = max(_TOTAL - elapsed, 1)
    return remaining / _TOTAL


def bs_fair_price(spot, strike, rv, dte_days, option_type, r=0.065, q=0.012):
    T = _fractional_dte(dte_days) / 365.0
    if rv <= 0.01:
        rv = 0.12
    if T <= 0:
        price = max(0.0, spot - strike) if option_type == "CE" else max(0.0, strike - spot)
        return max(0.01, price), 0.0
    try:
        d1 = (math.log(spot / strike) + (r - q + 0.5 * rv**2) * T) / (rv * math.sqrt(T))
        d2 = d1 - rv * math.sqrt(T)

        def _N(x):
            if x < -8: return 0.0
            if x >  8: return 1.0
            if x <  0: return 1.0 - _N(-x)
            k  = 1.0 / (1.0 + 0.2316419 * x)
            k2 = k * k
            p  = (0.319381530*k - 0.356563782*k2 + 1.781477937*k2*k
                  - 1.821255978*k2*k2 + 1.330274429*k2*k2*k)
            return 1.0 - math.exp(-0.5*x*x) / math.sqrt(2*math.pi) * p

        if option_type == "CE":
            price = spot * math.exp(-q*T) * _N(d1) - strike * math.exp(-r*T) * _N(d2)
            delta = math.exp(-q*T) * _N(d1)
        else:
            price = strike * math.exp(-r*T) * _N(-d2) - spot * math.exp(-q*T) * _N(-d1)
            delta = -math.exp(-q*T) * _N(-d1)
        return max(0.01, price), round(delta, 4)
    except Exception:
        return 0.01, 0.0


def get_atm_iv(chain_out, spot):
    """Returns average ATM implied vol (%) from chain rows."""
    atm = round(spot / 50) * 50
    row = next((r for r in chain_out if r["strike"] == atm), None)
    if not row:
        return 0.0
    ce_iv = float(row.get("ce_iv", 0) or 0)
    pe_iv = float(row.get("pe_iv", 0) or 0)
    if ce_iv > 0 and pe_iv > 0:
        return round((ce_iv + pe_iv) / 2, 2)
    return round(ce_iv or pe_iv, 2)


def overpricing_pct(ltp, fair):
    """Returns OVR% only when the option is liquid enough to trade and model is reliable.

    Suppresses:
      fair < 1.0  — BS fair is too small (rounding artefact); OVR% is meaningless
      ltp  < 0.50 — bid-ask spread kills any theoretical edge; not tradeable
    """
    if fair is None or fair < 1.0 or ltp is None or ltp < 0.50:
        return None
    return round((ltp - fair) / fair * 100, 1)

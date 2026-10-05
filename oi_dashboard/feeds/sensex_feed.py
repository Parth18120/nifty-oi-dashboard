import time

from config import ACCESS_TOKEN, CLIENT_ID
from dhanhq import DhanContext, dhanhq

dhan = dhanhq(DhanContext(CLIENT_ID, ACCESS_TOKEN))
SENSEX_SCRIP_ID = 51

_last_call = 0


def _throttle():
    global _last_call
    elapsed = time.time() - _last_call
    if elapsed < 3.5:
        time.sleep(3.5 - elapsed)
    _last_call = time.time()


def get_sensex_data():
    """Returns dict: {spot, expiry, dte, chain_rows} or None."""
    try:
        # Expiry list
        e = dhan.expiry_list(
            under_security_id=SENSEX_SCRIP_ID,
            under_exchange_segment='IDX_I'
        )
        if e.get('status') != 'success':
            return None
        expiries = e.get('data', {}).get('data', [])
        if not expiries:
            return None
        expiry = expiries[0]

        # DTE — fractional for intraday accuracy, integer for display
        from datetime import datetime, timedelta, timezone
        from datetime import time as _time
        _IST = timezone(timedelta(hours=5, minutes=30))
        now_ist = datetime.now(_IST)
        exp_dt  = datetime.strptime(expiry, '%Y-%m-%d')
        display_dte = max((exp_dt.date() - now_ist.date()).days, 0)

        # Pricing DTE: session-aware fraction of a trading day.
        # Pre-market (< 9:15): full session ahead → 1.0
        # In-session (9:15-15:30): decay by elapsed minutes
        # Post-market / non-expiry day: wall-clock / calendar days
        _OPEN  = _time(9, 15)
        _CLOSE = _time(15, 30)
        _TOTAL = 375   # total trading-session minutes
        _t     = now_ist.time()

        if display_dte > 0:
            # Non-expiry day: use calendar days as before
            pricing_dte = float(display_dte)
            _branch = 'CALENDAR'
        elif _t < _OPEN:
            # Expiry day, pre-market: full session's variance still ahead
            pricing_dte = 1.0
            _branch = 'PRE_MARKET'
        elif _t >= _CLOSE:
            # Expiry day, post-market: essentially expired
            pricing_dte = 0.001
            _branch = 'POST_MARKET'
        else:
            # Expiry day, in-session: decay by elapsed minutes
            _elapsed    = (now_ist.hour * 60 + now_ist.minute) - (9 * 60 + 15)
            _remaining  = max(_TOTAL - _elapsed, 1)
            pricing_dte = _remaining / _TOTAL
            _branch     = 'IN_SESSION'

        print(f'[SENSEX DTE] now={now_ist.strftime("%H:%M:%S")} branch={_branch} '
              f'display_dte={display_dte} pricing_dte={pricing_dte:.4f} T={pricing_dte/365:.6f}')

        # Chain (contains spot too)
        _throttle()
        c = dhan.option_chain(
            under_security_id=SENSEX_SCRIP_ID,
            under_exchange_segment='IDX_I',
            expiry=expiry
        )
        if c.get('status') != 'success':
            return None

        inner = c.get('data', {}).get('data', {})
        spot = inner.get('last_price', 0)
        if spot <= 0:
            return None

        oc = inner.get('oc', {})
        rows = []
        for strike_str, sd in oc.items():
            ce = sd.get('ce', {})
            pe = sd.get('pe', {})
            rows.append({
                'strike':  float(strike_str),
                'ce_ltp':  ce.get('last_price', 0),
                'ce_iv':   ce.get('implied_volatility', 0),
                'ce_oi':   ce.get('oi', 0),
                'pe_ltp':  pe.get('last_price', 0),
                'pe_iv':   pe.get('implied_volatility', 0),
                'pe_oi':   pe.get('oi', 0),
            })
        rows.sort(key=lambda r: r['strike'])

        return {
            'spot':        spot,
            'expiry':      expiry,
            'dte':         display_dte,     # integer for display (0, 1, 2 …)
            'pricing_dte': pricing_dte,     # fractional for Black-Scholes T
            'chain_rows':  rows,
        }

    except Exception as ex:
        print(f'[SENSEX] fetch error: {ex}')
        return None

"""
dhan_feed.py
All Dhan API data fetching for the OI dashboard.
Returns clean dicts ready for analysis and display.

API findings (tested 2026-06-29):
- All security IDs must be passed as integers, not strings
- Nifty 50 index: IDX_I segment, security_id=13
- India VIX:      IDX_I segment, security_id=21
- option_chain:   under_security_id=13 (int), under_exchange_segment="IDX_I"
  - Response path: r['data']['data']['oc']  keyed by "24200.000000"
  - Each strike:   {"ce": {last_price, oi, implied_volatility, greeks, ...}, "pe": {...}}
  - Underlying LTP also at r['data']['data']['last_price']
- expiry_list:    under_security_id=13, under_exchange_segment="IDX_I"
  - Data at r['data']['data'] (list of "YYYY-MM-DD")
"""

import threading
import time as _time
import traceback
from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

_dhan       = None
_prev_chain = {}   # {strike: {"ce_oi": x, "pe_oi": x}}

# ── Rate-limit protection ────────────────────────────────────────────────────
# Dhan blocks/throttles clients that call too frequently (esp. option_chain,
# which has a strict ~1-req/3-sec limit). This enforces a safe minimum gap
# between ANY two Dhan API calls made by this process, no matter which
# function triggers them, so a burst of fetches (e.g. spot + vix + expiry +
# chain all in one refresh) can never fire back-to-back.
_MIN_GAP_SECONDS   = 1.5
_last_call_time    = 0.0
_throttle_lock     = threading.Lock()


def _throttle():
    """Block just long enough to guarantee _MIN_GAP_SECONDS since the last
    Dhan API call anywhere in this process. Call this immediately before
    every dhan.<method>(...) invocation."""
    global _last_call_time
    with _throttle_lock:
        now      = _time.time()
        elapsed  = now - _last_call_time
        wait_for = _MIN_GAP_SECONDS - elapsed
        if wait_for > 0:
            _time.sleep(wait_for)
        _last_call_time = _time.time()


def _get_dhan():
    global _dhan
    if _dhan is None:
        from config import ACCESS_TOKEN, CLIENT_ID
        from dhanhq import DhanContext, dhanhq
        _dhan = dhanhq(DhanContext(CLIENT_ID, ACCESS_TOKEN))
    return _dhan


def reset_dhan_client():
    global _dhan
    _dhan = None


def fetch_nifty_spot():
    """
    Fetch Nifty 50 spot price using ohlc_data (IDX_I, security_id=13).
    Returns dict: ltp, open, high, low, prev_close, change, change_pct
    """
    try:
        dhan = _get_dhan()
        _throttle()
        resp = dhan.ohlc_data({"IDX_I": [13]})
        if resp and resp.get("status") == "success":
            inner = resp.get("data", {}).get("data", {})
            seg   = inner.get("IDX_I", {})
            d     = seg.get(13) or seg.get("13") or {}
            if not d or not d.get("last_price"):
                for _k, _v in seg.items():
                    if isinstance(_v, dict) and float(_v.get("last_price", 0) or 0) > 1000:
                        d = _v
                        break
            ltp        = float(d.get("last_price", 0) or 0)
            ohlc       = d.get("ohlc", {})
            open_p     = float(ohlc.get("open",  ltp))
            high_p     = float(ohlc.get("high",  ltp))
            low_p      = float(ohlc.get("low",   ltp))
            prev_close = float(ohlc.get("close", ltp))
            change     = ltp - prev_close
            change_pct = (change / prev_close * 100) if prev_close else 0.0
            if ltp > 0:
                return {
                    "ltp":        ltp,
                    "open":       open_p,
                    "high":       high_p,
                    "low":        low_p,
                    "prev_close": prev_close,
                    "change":     round(change, 2),
                    "change_pct": round(change_pct, 2),
                }
    except Exception as e:
        print(f"[dhan_feed] ERROR in {__name__}: {e}")
        traceback.print_exc()

    # Fallback: ticker_data
    try:
        dhan = _get_dhan()
        _throttle()
        resp = dhan.ticker_data({"IDX_I": [13]})
        if resp and resp.get("status") == "success":
            inner = resp.get("data", {}).get("data", {})
            seg   = inner.get("IDX_I", {})
            d     = seg.get(13) or seg.get("13") or {}
            if not d or not d.get("last_price"):
                for _k, _v in seg.items():
                    if isinstance(_v, dict) and float(_v.get("last_price", 0) or 0) > 1000:
                        d = _v
                        break
            ltp   = float(d.get("last_price", 0) or 0)
            if ltp > 0:
                return {
                    "ltp":        ltp,
                    "open":       ltp,
                    "high":       ltp,
                    "low":        ltp,
                    "prev_close": ltp,
                    "change":     0.0,
                    "change_pct": 0.0,
                }
    except Exception as e:
        print(f"[dhan_feed] ERROR in {__name__}: {e}")
        traceback.print_exc()

    return {
        "ltp": 0.0, "open": 0.0, "high": 0.0, "low": 0.0,
        "prev_close": 0.0, "change": 0.0, "change_pct": 0.0,
        "error": "failed to fetch Nifty spot",
    }


def fetch_india_vix():
    """
    Fetch India VIX via ticker_data (IDX_I, security_id=21).
    Returns float: current VIX value.
    """
    try:
        dhan  = _get_dhan()
        _throttle()
        resp  = dhan.ticker_data({"IDX_I": [21]})
        if resp and resp.get("status") == "success":
            inner = resp.get("data", {}).get("data", {})
            seg   = inner.get("IDX_I", {})
            d     = seg.get(21) or seg.get("21") or {}
            if not d:
                for _v in seg.values():
                    if isinstance(_v, dict) and float(_v.get("last_price", 0) or 0) > 0:
                        d = _v; break
            vix   = float(d.get("last_price", 0))
            return round(vix, 2)
    except Exception as e:
        print(f"[dhan_feed] ERROR in {__name__}: {e}")
        traceback.print_exc()
    return 0.0


def fetch_expiry_dates():
    """
    Return list of upcoming Nifty weekly expiry dicts.
    Each dict: {"expiry": "YYYY-MM-DD", "days_to_expiry": N}
    Falls back to computing from nearest Tuesday if API unavailable.
    """
    try:
        dhan  = _get_dhan()
        _throttle()
        resp  = dhan.expiry_list(
            under_security_id=13,
            under_exchange_segment="IDX_I",
        )
        if resp and resp.get("status") == "success":
            # data is double-nested: r['data']['data'] = list of date strings
            inner    = resp.get("data", {})
            expiries = inner.get("data", []) if isinstance(inner, dict) else inner
            today    = date.today()
            result   = []
            for exp in expiries:
                try:
                    exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
                    dte      = (exp_date - today).days
                    if dte >= 0:
                        result.append({
                            "expiry":         exp,
                            "days_to_expiry": dte,
                        })
                        if len(result) >= 4:
                            break
                except Exception:
                    continue
            if result:
                return result
    except Exception as e:
        print(f"[dhan_feed] ERROR in {__name__}: {e}")
        traceback.print_exc()

    # Fallback: compute nearest Tuesday
    today = date.today()
    days  = (1 - today.weekday()) % 7
    if days == 0:
        now = datetime.now(IST)
        if now.hour >= 15 and now.minute >= 30:
            days = 7
    first_exp = today + timedelta(days=days)
    result = []
    for i in range(4):
        exp_i = first_exp + timedelta(weeks=i)
        dte   = (exp_i - today).days
        result.append({
            "expiry":         exp_i.strftime("%Y-%m-%d"),
            "days_to_expiry": max(0, dte),
        })
    return result


def nearest_expiry() -> str:
    """
    Return the nearest Nifty weekly expiry that is >= today (includes today).
    Used for OI analysis on expiry day — today's chain has all the open interest.
    Falls back to computing nearest Tuesday if API unavailable.
    """
    today = date.today()
    for e in fetch_expiry_dates():
        try:
            exp_date = datetime.strptime(e["expiry"], "%Y-%m-%d").date()
            if exp_date >= today:
                return e["expiry"]
        except (ValueError, TypeError):
            continue
    # Fallback: nearest Tuesday including today
    days = (1 - today.weekday()) % 7
    if days == 0:
        now = datetime.now(IST)
        if now.hour >= 15 and now.minute >= 30:
            days = 7
    return (today + timedelta(days=days)).strftime("%Y-%m-%d")


def fetch_option_chain(spot):
    """
    Fetch full option chain for nearest weekly expiry.
    Returns list of dicts sorted by strike ascending.
    OI change is computed vs previous fetch (_prev_chain).
    On first fetch, oi_chg = 0 for all strikes.

    option_chain response structure:
      r['data']['data']['last_price']  = underlying LTP
      r['data']['data']['oc']          = {
          "24200.000000": {
              "ce": {last_price, oi, implied_volatility, greeks: {delta,gamma,theta,vega},
                     average_price, previous_close, volume, bid_price, ask_price},
              "pe": { same fields }
          }, ...
      }
    """
    global _prev_chain

    expiries  = fetch_expiry_dates()

    # Expiry selection: Tuesday = roll to next week; all other days = current week.
    # DTE=0 is never allowed (expiry day itself is skipped).
    today        = date.today()
    is_tuesday   = today.weekday() == 1  # Mon=0 Tue=1
    exp_dates    = sorted(
        datetime.strptime(e["expiry"], "%Y-%m-%d").date()
        for e in expiries
        if e["expiry"]
    )

    if is_tuesday:
        # Tuesday: skip this week's expiry (today or earlier), take next week
        chosen = next((e for e in exp_dates if e > today), None)
    else:
        # Mon/Wed/Thu/Fri: use nearest expiry that is NOT today (DTE>=1)
        chosen = next((e for e in exp_dates if e > today), None)

    if not chosen:
        chosen = today + timedelta(days=(1 - today.weekday()) % 7 or 7)

    expiry     = chosen.strftime("%Y-%m-%d")
    chosen_dte = (chosen - today).days
    print(f"[EXPIRY] today={today} ({today.strftime('%A')}) "
          f"chosen={chosen} dte={chosen_dte} tuesday_roll={is_tuesday}")
    print(f"[dhan_feed] fetch_option_chain expiry={expiry}")

    oc_data   = {}
    chain_ltp = spot

    try:
        dhan = _get_dhan()
        _throttle()
        resp = dhan.option_chain(
            under_security_id=13,
            under_exchange_segment="IDX_I",
            expiry=expiry,
        )
        if resp and resp.get("status") == "success":
            inner     = resp.get("data", {}).get("data", {})
            chain_ltp = float(inner.get("last_price", spot) or spot)
            oc_data   = inner.get("oc", {})
    except Exception as e:
        print(f"[dhan_feed] ERROR in {__name__}: {e}")
        traceback.print_exc()

    # Filter strikes near ATM
    from config import STRIKES_TO_SHOW
    atm  = round(chain_ltp / 50) * 50
    step = 50
    lo   = atm - STRIKES_TO_SHOW * step
    hi   = atm + STRIKES_TO_SHOW * step

    def _iv(raw):
        """Normalise IV: decimal (0.135) → percentage (13.5), or pass through if already %."""
        v = float(raw or 0)
        return round(v * 100, 2) if 0 < v < 2 else round(v, 2)

    chain = []
    for key, val in oc_data.items():
        try:
            strike = int(round(float(key)))
        except (ValueError, TypeError):
            continue
        if not (lo <= strike <= hi):
            continue

        ce    = val.get("ce", {}) or {}
        pe    = val.get("pe", {}) or {}
        ce_oi = int(ce.get("oi", 0) or 0)
        pe_oi = int(pe.get("oi", 0) or 0)
        prev  = _prev_chain.get(strike, {})
        ce_oi_chg = ce_oi - prev.get("ce_oi", ce_oi)
        pe_oi_chg = pe_oi - prev.get("pe_oi", pe_oi)

        chain.append({
            "strike":     strike,
            "ce_ltp":     round(float(ce.get("last_price", 0) or 0), 2),
            "ce_iv":      _iv(ce.get("implied_volatility", 0)),
            "ce_oi":      ce_oi,
            "ce_oi_prev": prev.get("ce_oi", ce_oi),
            "ce_oi_chg":  ce_oi_chg,
            "ce_volume":  int(ce.get("volume", 0) or 0),
            "ce_bid":     round(float(ce.get("bid_price", 0) or 0), 2),
            "ce_ask":     round(float(ce.get("ask_price", 0) or 0), 2),
            "pe_ltp":     round(float(pe.get("last_price", 0) or 0), 2),
            "pe_iv":      _iv(pe.get("implied_volatility", 0)),
            "pe_oi":      pe_oi,
            "pe_oi_prev": prev.get("pe_oi", pe_oi),
            "pe_oi_chg":  pe_oi_chg,
            "pe_volume":  int(pe.get("volume", 0) or 0),
            "pe_bid":     round(float(pe.get("bid_price", 0) or 0), 2),
            "pe_ask":     round(float(pe.get("ask_price", 0) or 0), 2),
        })

    _prev_chain = {r["strike"]: {"ce_oi": r["ce_oi"], "pe_oi": r["pe_oi"]} for r in chain}

    return sorted(chain, key=lambda r: r["strike"])


def fetch_option_chain_for_expiry(spot, expiry_date):
    """
    Fetch option chain for a specific expiry date.
    Updates _prev_chain so OI change deltas accumulate across refreshes.
    Returns list of dicts sorted by strike ascending (same schema as fetch_option_chain).
    """
    global _prev_chain
    oc_data   = {}
    chain_ltp = spot

    try:
        dhan = _get_dhan()
        _throttle()
        resp = dhan.option_chain(
            under_security_id=13,
            under_exchange_segment="IDX_I",
            expiry=expiry_date,
        )
        if resp and resp.get("status") == "success":
            inner     = resp.get("data", {}).get("data", {})
            chain_ltp = float(inner.get("last_price", spot) or spot)
            oc_data   = inner.get("oc", {})
    except Exception as e:
        print(f"[dhan_feed] ERROR in {__name__}: {e}")
        traceback.print_exc()

    from config import STRIKES_TO_SHOW
    atm  = round(chain_ltp / 50) * 50
    step = 50
    lo   = atm - STRIKES_TO_SHOW * step
    hi   = atm + STRIKES_TO_SHOW * step

    def _iv(raw):
        v = float(raw or 0)
        return round(v * 100, 2) if 0 < v < 2 else round(v, 2)

    chain = []
    for key, val in oc_data.items():
        try:
            strike = int(round(float(key)))
        except (ValueError, TypeError):
            continue
        if not (lo <= strike <= hi):
            continue

        ce = val.get("ce", {}) or {}
        pe = val.get("pe", {}) or {}
        ce_oi = int(ce.get("oi", 0) or 0)
        pe_oi = int(pe.get("oi", 0) or 0)
        prev  = _prev_chain.get(strike, {})
        ce_oi_chg = ce_oi - prev.get("ce_oi", ce_oi)   # 0 on first fetch
        pe_oi_chg = pe_oi - prev.get("pe_oi", pe_oi)   # 0 on first fetch
        chain.append({
            "strike":      strike,
            "ce_ltp":      round(float(ce.get("last_price", 0) or 0), 2),
            "ce_iv":       _iv(ce.get("implied_volatility", 0)),
            "ce_oi":       ce_oi,
            "ce_oi_prev":  prev.get("ce_oi", ce_oi),
            "ce_oi_chg":   ce_oi_chg,
            "ce_volume":   int(ce.get("volume", 0) or 0),
            "ce_bid":      round(float(ce.get("bid_price", 0) or 0), 2),
            "ce_ask":      round(float(ce.get("ask_price", 0) or 0), 2),
            "pe_ltp":      round(float(pe.get("last_price", 0) or 0), 2),
            "pe_iv":       _iv(pe.get("implied_volatility", 0)),
            "pe_oi":       pe_oi,
            "pe_oi_prev":  prev.get("pe_oi", pe_oi),
            "pe_oi_chg":   pe_oi_chg,
            "pe_volume":   int(pe.get("volume", 0) or 0),
            "pe_bid":      round(float(pe.get("bid_price", 0) or 0), 2),
            "pe_ask":      round(float(pe.get("ask_price", 0) or 0), 2),
        })

    _prev_chain = {r["strike"]: {"ce_oi": r["ce_oi"], "pe_oi": r["pe_oi"]} for r in chain}
    return sorted(chain, key=lambda r: r["strike"])


def fetch_strike_prices(expiry_str, strike_pairs, retries=2):
    """
    Targeted fetch for specific option strikes with NO ATM range filter.
    Used for hedge/buy legs that may be outside the chain snapshot window.

    strike_pairs: list of (strike_int, 'CE'|'PE') tuples
    Returns: dict keyed by (strike, side) -> {ltp, bid, ask, volume, oi}
    Returns empty dict on failure (caller decides whether to abort).
    """
    result = {}
    if not strike_pairs:
        return result

    def _iv(raw):
        v = float(raw or 0)
        return round(v * 100, 2) if 0 < v < 2 else round(v, 2)

    needed = {(int(s), side.upper()) for s, side in strike_pairs}

    for attempt in range(retries):
        try:
            dhan = _get_dhan()
            _throttle()
            resp = dhan.option_chain(
                under_security_id=13,
                under_exchange_segment="IDX_I",
                expiry=expiry_str,
            )
            if resp and resp.get("status") == "success":
                oc_data = resp.get("data", {}).get("data", {}).get("oc", {})
                for key, val in oc_data.items():
                    try:
                        strike = int(round(float(key)))
                    except (ValueError, TypeError):
                        continue
                    for side_key, side_label in (("ce", "CE"), ("pe", "PE")):
                        if (strike, side_label) not in needed:
                            continue
                        leg = val.get(side_key, {}) or {}
                        result[(strike, side_label)] = {
                            "ltp":    round(float(leg.get("last_price", 0) or 0), 2),
                            "bid":    round(float(leg.get("bid_price",  0) or 0), 2),
                            "ask":    round(float(leg.get("ask_price",  0) or 0), 2),
                            "volume": int(leg.get("volume", 0) or 0),
                            "oi":     int(leg.get("oi",     0) or 0),
                            "iv":     _iv(leg.get("implied_volatility", 0)),
                        }
                if len(result) >= len(needed):
                    break
        except Exception as e:
            print(f"[fetch_strike_prices] attempt {attempt+1} failed: {e}")
            if attempt < retries - 1:
                _time.sleep(2)

    missing = needed - set(result.keys())
    if missing:
        print(f"[fetch_strike_prices] WARNING: no data for {missing}")
    return result


_last_banknifty = 0.0   # last valid BankNifty reading for fallback


def fetch_banknifty_spot():
    """
    Fetch BankNifty spot LTP (IDX_I, security_id=25).
    Returns float LTP, or 0.0 on failure.
    """
    global _last_banknifty
    try:
        dhan = _get_dhan()
        _throttle()
        resp = dhan.ohlc_data({"IDX_I": [25]})
        if resp and resp.get("status") == "success":
            inner = resp.get("data", {}).get("data", {})
            seg   = inner.get("IDX_I", {})
            d     = seg.get(25) or seg.get("25") or {}
            if not d or not d.get("last_price"):
                for _k, _v in seg.items():
                    if isinstance(_v, dict) and float(_v.get("last_price", 0) or 0) > 10000:
                        d = _v; break
            ltp = float(d.get("last_price", 0) or 0)
            if ltp > 0:
                _last_banknifty = ltp
                return ltp
    except Exception as e:
        print(f"[dhan_feed] fetch_banknifty_spot: {e}")
    # ticker_data fallback
    try:
        dhan = _get_dhan()
        _throttle()
        resp = dhan.ticker_data({"IDX_I": [25]})
        if resp and resp.get("status") == "success":
            inner = resp.get("data", {}).get("data", {})
            seg   = inner.get("IDX_I", {})
            d     = seg.get(25) or seg.get("25") or {}
            ltp   = float(d.get("last_price", 0) or 0)
            if ltp > 0:
                _last_banknifty = ltp
                return ltp
    except Exception as e:
        print(f"[dhan_feed] fetch_banknifty_spot fallback: {e}")
    return _last_banknifty or 0.0


def fetch_nifty_minute_bars(count=30):
    """
    Fetch last N minute bars for NIFTY from Dhan intraday data.
    Returns list of {time, open, high, low, close, volume} dicts,
    newest last. Empty list on failure.
    """
    from datetime import datetime, timedelta, timezone
    try:
        dhan      = _get_dhan()
        _throttle()
        today_str = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d")
        resp = dhan.intraday_minute_data(
            security_id      = "13",
            exchange_segment = "IDX_I",
            instrument_type  = "INDEX",
            from_date        = today_str,
            to_date          = today_str,
            interval         = 1,
        )
        if not (resp and resp.get("status") == "success"):
            print(f"[dhan_feed] minute_bars: {resp.get('remarks') if resp else 'no response'}")
            return []
        data = resp.get("data", {})
        # Dhan returns parallel arrays: open[], high[], low[], close[], volume[], timestamp[]
        opens     = data.get("open",      [])
        highs     = data.get("high",      [])
        lows      = data.get("low",       [])
        closes    = data.get("close",     [])
        volumes   = data.get("volume",    [])
        stamps    = data.get("timestamp", data.get("start_Time", []))
        bars = []
        n = min(len(closes), len(stamps))
        for i in range(n):
            try:
                ts = stamps[i]
                if isinstance(ts, (int, float)):
                    dt = datetime.utcfromtimestamp(ts).replace(
                        tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=5, minutes=30)))
                else:
                    dt = datetime.fromisoformat(str(ts))
                bars.append({
                    "time":   dt,
                    "open":   float(opens[i]   if opens   else closes[i]),
                    "high":   float(highs[i]   if highs   else closes[i]),
                    "low":    float(lows[i]    if lows    else closes[i]),
                    "close":  float(closes[i]),
                    "volume": int(volumes[i]   if volumes else 0),
                })
            except Exception:
                pass
        return bars[-count:] if len(bars) > count else bars
    except Exception as e:
        print(f"[dhan_feed] fetch_nifty_minute_bars: {e}")
        return []


if __name__ == "__main__":
    import time

    print("Testing fetch_nifty_spot...")
    spot_data = fetch_nifty_spot()
    print(f"Spot: {spot_data}")
    time.sleep(2)

    print("\nTesting fetch_india_vix...")
    vix = fetch_india_vix()
    print(f"VIX: {vix}")
    time.sleep(2)

    print("\nTesting fetch_expiry_dates...")
    expiries = fetch_expiry_dates()
    print(f"Expiries: {expiries}")
    time.sleep(2)

    print("\nTesting fetch_option_chain...")
    chain = fetch_option_chain(spot_data["ltp"])
    print(f"Chain rows: {len(chain)}")
    if chain:
        mid = len(chain) // 2
        print(f"ATM area row: {chain[mid]}")

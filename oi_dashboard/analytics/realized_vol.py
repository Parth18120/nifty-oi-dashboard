"""Realised-volatility estimators (Yang-Zhang, close-to-close) and blended sigma selection."""

import csv
import math
from datetime import datetime
from pathlib import Path

from oi_dashboard.core import state
from oi_dashboard.core.constants import IST, SPOT_HISTORY_FILE
from oi_dashboard.core.util import parse_date_safe


def load_spot_history():
    """
    Load last 65 trading days of OHLC from spot_all.csv.
    Normalizes mixed date formats (DD-MM-YYYY, YYYY-MM-DD) by parsing to date objects.
    Sorts by date object — immune to lexicographic ordering bugs.
    """

    if not SPOT_HISTORY_FILE or not Path(SPOT_HISTORY_FILE).exists():
        print(f"[RV] Spot history file not found: {SPOT_HISTORY_FILE}")
        print("[RV] RV will accumulate from live data (~20 trading days to stabilize)")
        return

    try:
        rows = []
        bad_dates = 0
        with open(SPOT_HISTORY_FILE, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                raw_date = row.get("date", "") or row.get("datetime", "")
                d = parse_date_safe(raw_date)
                if d is None:
                    bad_dates += 1
                    if bad_dates <= 3:
                        print(f"[RV] SKIP bad date: {raw_date!r}")
                    continue
                try:
                    close = float(row.get("close", 0) or 0)
                    if close < 100:
                        continue
                    open_p = float(row.get("open",  close) or close)
                    high_p = float(row.get("high",  close) or close)
                    low_p  = float(row.get("low",   close) or close)
                    rows.append({
                        "date":  d.isoformat(),   # always YYYY-MM-DD
                        "dt":    d,               # date object for sort
                        "open":  open_p,
                        "high":  high_p,
                        "low":   low_p,
                        "close": close,
                    })
                except (ValueError, TypeError) as e:
                    print(f"[RV] SKIP bad row {raw_date!r}: {e}")

        if bad_dates > 3:
            print(f"[RV] ... ({bad_dates} total bad-date rows skipped)")

        if not rows:
            print("[RV] No valid data found in spot history file")
            return

        # Sort by date OBJECT — fixes DD-MM-YYYY vs YYYY-MM-DD lexicographic bug
        rows.sort(key=lambda x: x["dt"])
        rows = rows[-65:]  # keep last 65 trading days

        state.ohlc_history       = rows
        state.spot_history       = [r["close"] for r in rows]
        state.spot_history_dates = [r["date"]  for r in rows]

        print(f"[RV] Loaded {len(rows)} days: {rows[0]['date']} → {rows[-1]['date']}")
        print(f"[RV] Last 3 dates: {' | '.join(r['date'] for r in rows[-3:])}")
        print(f"[RV] Last close: {state.spot_history[-1]:.2f}")

        _compute_all_rvs()

    except Exception as e:
        print(f"[RV] Failed to load spot history: {e}")
        import traceback
        traceback.print_exc()


def yang_zhang(rows, window=20):
    """
    Yang-Zhang OHLC volatility estimator (annualized fraction, e.g. 0.1132).
    Captures both overnight gaps (close→open) and intraday range (RS term).
    Requires list of {open,high,low,close} dicts in chronological order.
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


def _compute_all_rvs():
    """
    Compute RV for all windows: RV5, RV10, RV20, RV60 and YZ20.
    Called after loading history and after each daily close update.
    """

    if len(state.spot_history) < 6:
        print("[RV] Insufficient history for RV computation")
        return

    today_str = datetime.now(IST).strftime("%Y-%m-%d")

    def rv_window(n):
        if len(state.spot_history) < n + 1:
            return None
        prices   = state.spot_history[-(n + 1):]
        log_rets = [math.log(prices[i] / prices[i-1])
                    for i in range(1, len(prices))
                    if prices[i-1] > 0 and prices[i] > 0]
        if len(log_rets) < 2:
            return None
        mean_r   = sum(log_rets) / len(log_rets)
        variance = sum((r - mean_r)**2 for r in log_rets) / (len(log_rets) - 1)
        return round(math.sqrt(variance * 252), 4)

    rv5  = rv_window(5)
    rv10 = rv_window(10)
    rv20 = rv_window(20)
    rv60 = rv_window(60)
    yz20 = yang_zhang(state.ohlc_history, window=20) if len(state.ohlc_history) >= 21 else None

    # EWMA vol (RiskMetrics lam=0.94)
    _ewma_closes = [r["close"] for r in state.ohlc_history if r.get("close", 0) > 0]
    ewma = None
    if len(_ewma_closes) >= 3:
        _lam = 0.94
        _var = (_ewma_closes[1] / _ewma_closes[0] - 1) ** 2 if _ewma_closes[0] > 0 else 0.0
        for _i in range(1, len(_ewma_closes)):
            _r = math.log(_ewma_closes[_i] / _ewma_closes[_i - 1]) if _ewma_closes[_i - 1] > 0 else 0.0
            _var = _lam * _var + (1 - _lam) * _r ** 2
        ewma = round(math.sqrt(_var * 252), 4)

    state.rv_cache[today_str] = {
        "rv5":  rv5,
        "rv10": rv10,
        "rv20": rv20,
        "rv60": rv60,
        "yz20": yz20,
        "ewma": ewma,
    }

    # Keep backward compat
    if rv20:
        state.rv20_cache[today_str] = rv20

    state.last_rv_date = today_str

    parts = []
    if rv5:  parts.append(f"RV5={rv5*100:.1f}%")
    if rv10: parts.append(f"RV10={rv10*100:.1f}%")
    if rv20: parts.append(f"RV20={rv20*100:.1f}%")
    if rv60: parts.append(f"RV60={rv60*100:.1f}%")
    if yz20: parts.append(f"YZ20={yz20*100:.1f}%")
    if ewma: parts.append(f"EWMA={ewma*100:.1f}%")
    if parts:
        print(f"[RV] {today_str}: {' | '.join(parts)}")

    # Vol model audit — printed once per server run (first call with full history)
    if not state.vol_audit_printed and len(state.spot_history) > 20:
        state.vol_audit_printed = True
        sigma = get_best_sigma()
        print("=== VOL MODEL AUDIT ===")
        print(f"  History : {len(state.spot_history)} days  {state.spot_history_dates[0] if state.spot_history_dates else 'n/a'} → {state.spot_history_dates[-1] if state.spot_history_dates else 'n/a'}")
        print(f"  RV5={rv5*100:.2f}%  RV20={rv20*100:.2f}%  RV60={rv60*100:.2f}%  YZ20={yz20*100:.2f}%" if all([rv5, rv20, rv60, yz20]) else f"  RVs: {parts}")
        print(f"  sigma_used={sigma*100:.2f}%  (src: {'YZ20' if yz20 and abs(sigma - yz20) < 0.0001 else 'other'})  VIX={state.last_vix:.1f}%")
        print("========================")


def update_rv20(spot_ltp):
    """
    Update spot history with today's closing price and recompute all RVs.
    Called on every data refresh but only adds one entry per trading day.
    Returns RV20 value for backward compatibility.
    """

    now       = datetime.now(IST)
    today_str = now.strftime("%Y-%m-%d")

    # Only update once per trading day
    if state.last_rv_date == today_str:
        return state.rv_cache.get(today_str, {}).get("rv20") or 0.0

    # Add today's close if not already present
    if spot_ltp and spot_ltp > 100:
        c = float(spot_ltp)
        if not state.spot_history_dates or state.spot_history_dates[-1] != today_str:
            state.spot_history.append(c)
            state.spot_history_dates.append(today_str)
            # Stub OHLC row using close for all fields (no intraday data here)
            from datetime import date as _date
            state.ohlc_history.append({"date": today_str, "dt": _date.fromisoformat(today_str),
                                   "open": c, "high": c, "low": c, "close": c})
            if len(state.spot_history) > 70:
                state.spot_history       = state.spot_history[-70:]
                state.spot_history_dates = state.spot_history_dates[-70:]
                state.ohlc_history       = state.ohlc_history[-70:]

    _compute_all_rvs()
    return state.rv_cache.get(today_str, {}).get("rv20") or 0.0


def get_rv20():
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    rv20 = state.rv_cache.get(today_str, {}).get("rv20")
    return rv20 if rv20 else 0.0


def get_all_rvs():
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    return state.rv_cache.get(today_str, {
        "rv5": None, "rv10": None, "rv20": None, "rv60": None, "yz20": None, "ewma": None
    })


def get_best_sigma():
    rvs  = get_all_rvs()
    rv5  = rvs.get("rv5")  or 0.0
    rv20 = rvs.get("rv20") or 0.0
    yz20 = rvs.get("yz20") or 0.0
    ewma = rvs.get("ewma") or 0.0

    # EVENT SPIKE FILTER: RV5 > 1.5x RV20 means yesterday was a crash/rally day.
    # Use YZ20 (or RV20) single-selector to avoid inflating fair value.
    event_spike = (rv5 > 0 and rv20 > 0 and rv5 > rv20 * 1.5)
    if event_spike:
        print(f"[SIGMA] Event spike: RV5={rv5*100:.1f}% > 1.5×RV20={rv20*100:.1f}% — using YZ20/RV20")
        sigma = yz20 if yz20 >= 0.06 else rv20
    else:
        # Weighted blend: RV20 replaced RV60 as anchor — backtested lower RMSE (4.11 vs 4.39) on 124-day NIFTY data.
        ew = 0.40 if ewma > 0 else 0.0
        yw = 0.30 if yz20 > 0 else 0.0
        rw = 0.30 if rv20 > 0 else 0.0
        total_w = ew + yw + rw
        if total_w > 0:
            sigma = (ewma * ew + yz20 * yw + rv20 * rw) / total_w
        else:
            sigma = max((state.last_vix or 11.8) / 100.0 * 0.85, 0.08)
            print(f"[SIGMA] All RV windows low — VIX fallback: {sigma*100:.1f}%")

    # VIX floor — sigma should not drop below 80% of VIX
    if state.last_vix and state.last_vix > 0:
        vix_floor = (state.last_vix / 100.0) * 0.80
        if sigma < vix_floor:
            sigma = vix_floor

    sigma = max(sigma, 0.08)
    if sigma > 0.30:
        print(f"[WARN] sigma {sigma*100:.1f}% unusually high — check OHLC data quality")
    return round(sigma, 4)

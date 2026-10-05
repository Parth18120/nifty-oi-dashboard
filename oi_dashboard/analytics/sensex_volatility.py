"""
sensex_volatility.py
Real realized-volatility computation for Sensex using sensex_spot_all.csv.
Mirrors Nifty's method in analytics.realized_vol exactly — same Yang-Zhang formula,
same fallback priority (YZ20 > RV20 > RV5).
"""
import csv
import math
from pathlib import Path

from oi_dashboard.core.paths import DATA_DIR

SENSEX_CSV = DATA_DIR / "sensex_spot_all.csv"


# ── Yang-Zhang OHLC estimator — same as analytics.realized_vol.yang_zhang ───────────────
# (Kept standalone so the Sensex path has no dependency on Nifty state.)

def _yang_zhang(rows, window=20):
    """
    Yang-Zhang OHLC volatility estimator (annualized fraction).
    rows: list of {open,high,low,close} dicts, chronological order.
    """
    if len(rows) < window + 1:
        return None
    data = rows[-(window + 1):]
    n    = window

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

    k      = 0.34 / (1.34 + (n + 1) / (n - 1))
    yz_var = _var(overnight) + k * _var(daytime) + (1 - k) * (sum(rs_terms) / len(rs_terms))
    return round(math.sqrt(yz_var * 252), 4) if yz_var > 0 else None


def _rv_close_to_close(rows, window=20):
    """Close-to-close RV — same formula as the Nifty close-to-close estimator."""
    if len(rows) < window + 1:
        return None
    closes   = [r["close"] for r in rows[-(window + 1):]]
    log_rets = [math.log(closes[i] / closes[i-1])
                for i in range(1, len(closes))
                if closes[i-1] > 0 and closes[i] > 0]
    if len(log_rets) < 2:
        return None
    mean_r   = sum(log_rets) / len(log_rets)
    variance = sum((r - mean_r)**2 for r in log_rets) / (len(log_rets) - 1)
    return round(math.sqrt(variance * 252), 4)


# ── CSV loader ────────────────────────────────────────────────────────────────

def _load_rows() -> list:
    """Returns list of {date,open,high,low,close} dicts, sorted chronologically.
    CSV dates are DD-MM-YYYY — must parse explicitly; lexicographic sort is wrong.
    """
    if not SENSEX_CSV.exists():
        return []
    from datetime import datetime as _dt
    rows = []
    with open(SENSEX_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                close = float(row["close"])
                if close < 1000:
                    continue
                rows.append({
                    "date":  row["date"],
                    "open":  float(row["open"]),
                    "high":  float(row["high"]),
                    "low":   float(row["low"]),
                    "close": close,
                    "_dt":   _dt.strptime(row["date"], "%d-%m-%Y"),
                })
            except (KeyError, ValueError):
                continue
    rows.sort(key=lambda r: r["_dt"])
    for r in rows:
        del r["_dt"]   # clean up the sort key before returning
    return rows


# ── Public API ────────────────────────────────────────────────────────────────

_MAX_PLAUSIBLE_SIGMA = 0.60   # 60% annualized — anything above is a calc error


def get_sensex_sigma():
    """
    Returns (sigma, source_str) using the same priority as Nifty:
      1. Yang-Zhang 20-day (preferred — captures overnight gaps)
      2. Close-to-close RV20 (fallback)
      3. (None, 'unavailable') — caller must handle

    Sanity clamp: rejects sigma > 60% (indicates data/calc error).
    """
    rows = _load_rows()
    if not rows:
        return None, "no_csv"

    yz = _yang_zhang(rows, window=20)
    if yz and 0.04 < yz <= _MAX_PLAUSIBLE_SIGMA:
        return yz, "yz20"
    if yz and yz > _MAX_PLAUSIBLE_SIGMA:
        print(f"[SENSEX SIGMA] REJECTED yz20={yz:.4f} (>{_MAX_PLAUSIBLE_SIGMA:.0%}), "
              f"trying rv20 — bad data in CSV likely cause")

    rv20 = _rv_close_to_close(rows, window=20)
    if rv20 and 0.04 < rv20 <= _MAX_PLAUSIBLE_SIGMA:
        return rv20, "rv20"
    if rv20 and rv20 > _MAX_PLAUSIBLE_SIGMA:
        print(f"[SENSEX SIGMA] REJECTED rv20={rv20:.4f} (>{_MAX_PLAUSIBLE_SIGMA:.0%})")

    return None, "unavailable"


def get_sensex_rv_all():
    """All RV windows — for diagnostics."""
    rows = _load_rows()
    return {
        "yz20":  _yang_zhang(rows, 20),
        "rv5":   _rv_close_to_close(rows, 5),
        "rv20":  _rv_close_to_close(rows, 20),
        "rv60":  _rv_close_to_close(rows, 60),
        "n_rows": len(rows),
    }

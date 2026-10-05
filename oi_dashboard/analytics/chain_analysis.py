"""
chain_analysis.py
Computes PCR, max pain, support/resistance, OI analysis
from the option chain list returned by dhan_feed.py
"""

# Module level OI history tracking
_oi_history     = {}   # {strike: [{time, ce_oi, pe_oi, ce_ltp, pe_ltp}]}
_pcr_history    = []   # [{time, pcr}]
_open_oi        = {}   # {strike: {ce_oi, pe_oi}} -- first fetch of session


def get_oi_history():
    return _oi_history


def get_pcr_history():
    return _pcr_history


def get_open_oi():
    return _open_oi


def update_oi_history(chain, timestamp):
    global _open_oi
    for row in chain:
        s = row["strike"]
        if s not in _oi_history:
            _oi_history[s] = []
        _oi_history[s].append({
            "time":   timestamp,
            "ce_oi":  row["ce_oi"],
            "pe_oi":  row["pe_oi"],
            "ce_ltp": row["ce_ltp"],
            "pe_ltp": row["pe_ltp"],
        })
        if len(_oi_history[s]) > 50:
            _oi_history[s] = _oi_history[s][-50:]
        # Record open OI on first fetch
        if s not in _open_oi:
            _open_oi[s] = {"ce_oi": row["ce_oi"], "pe_oi": row["pe_oi"]}


def update_pcr_history(pcr, timestamp):
    _pcr_history.append({"time": timestamp, "pcr": pcr})
    if len(_pcr_history) > 50:
        del _pcr_history[:-50]


def compute_pcr(chain):
    """
    Put Call Ratio = total PE OI / total CE OI
    > 1.3 = bearish sentiment (puts heavy), < 0.7 = bullish (calls heavy)
    """
    total_ce_oi = sum(r["ce_oi"] for r in chain)
    total_pe_oi = sum(r["pe_oi"] for r in chain)
    if total_ce_oi == 0:
        return 0.0
    return round(total_pe_oi / total_ce_oi, 3)


def pcr_label(pcr):
    if pcr >= 1.5:
        return "EXTREME BULLISH -- puts heavily sold, strong support", "green"
    if pcr >= 1.3:
        return "HIGH PCR -- bullish, good time to sell puts", "green"
    if pcr >= 1.0:
        return "MILD BULLISH BIAS", "yellow"
    if pcr >= 0.8:
        return "NEUTRAL", "white"
    if pcr >= 0.7:
        return "MILD BEARISH BIAS", "yellow"
    return "EXTREME GREED -- call heavy, bearish signal", "red"


def compute_max_pain(chain):
    """
    Max pain = strike where total loss to option buyers is maximum.
    For each potential expiry strike S:
        CE loss = sum of max(0, strike - S) * ce_oi for all strikes below S
        PE loss = sum of max(0, S - strike) * pe_oi for all strikes above S
    Max pain = S where total payout to buyers is minimum.
    """
    strikes  = [r["strike"] for r in chain]
    min_pain = float("inf")
    max_pain_strike = strikes[len(strikes) // 2] if strikes else 0

    for s in strikes:
        ce_loss = sum(max(0, r["strike"] - s) * r["ce_oi"] for r in chain)
        pe_loss = sum(max(0, s - r["strike"]) * r["pe_oi"] for r in chain)
        total   = ce_loss + pe_loss
        if total < min_pain:
            min_pain        = total
            max_pain_strike = s

    return max_pain_strike


def find_support_resistance(chain):
    """
    Support    = strikes with highest PE OI (put writers defending level)
    Resistance = strikes with highest CE OI (call writers defending level)
    Returns top 3 of each.
    """
    sorted_by_pe = sorted(chain, key=lambda r: r["pe_oi"], reverse=True)
    sorted_by_ce = sorted(chain, key=lambda r: r["ce_oi"], reverse=True)
    support    = [r["strike"] for r in sorted_by_pe[:3]]
    resistance = [r["strike"] for r in sorted_by_ce[:3]]
    return support, resistance


def compute_oi_buildup(chain):
    """
    Find strikes with significant OI change in this refresh.
    Returns sorted list of notable OI changes with interpretation.
    """
    notable = []
    for r in chain:
        ce_chg = r.get("ce_oi_chg", 0)
        pe_chg = r.get("pe_oi_chg", 0)

        threshold = 500_000
        if ce_chg > threshold:
            notable.append({
                "strike": r["strike"], "side": "CE", "change": ce_chg,
                "type": "BUILDUP",
                "note": f"CE {r['strike']} +{ce_chg:,} -- resistance building",
            })
        elif ce_chg < -threshold:
            notable.append({
                "strike": r["strike"], "side": "CE", "change": ce_chg,
                "type": "UNWIND",
                "note": f"CE {r['strike']} {ce_chg:,} -- resistance weakening",
            })
        if pe_chg > threshold:
            notable.append({
                "strike": r["strike"], "side": "PE", "change": pe_chg,
                "type": "BUILDUP",
                "note": f"PE {r['strike']} +{pe_chg:,} -- support building",
            })
        elif pe_chg < -threshold:
            notable.append({
                "strike": r["strike"], "side": "PE", "change": pe_chg,
                "type": "UNWIND",
                "note": f"PE {r['strike']} {pe_chg:,} -- support weakening",
            })

    result = sorted(notable, key=lambda x: abs(x["change"]), reverse=True)
    # Always show top 6 by |change| — even if all deltas are zero
    # (first refresh, or market closed — no filter on != 0)
    if not result:
        all_changes = []
        for r in chain:
            ce_chg = r.get("ce_oi_chg", 0)
            pe_chg = r.get("pe_oi_chg", 0)
            all_changes.append({"strike": r["strike"], "side": "CE", "change": ce_chg,
                "type": "BUILDUP" if ce_chg >= 0 else "UNWIND",
                "note": f"CE {r['strike']} {ce_chg:+,}" if ce_chg != 0 else f"CE {r['strike']} (unchanged)"})
            all_changes.append({"strike": r["strike"], "side": "PE", "change": pe_chg,
                "type": "BUILDUP" if pe_chg >= 0 else "UNWIND",
                "note": f"PE {r['strike']} {pe_chg:+,}" if pe_chg != 0 else f"PE {r['strike']} (unchanged)"})
        result = sorted(all_changes, key=lambda x: abs(x["change"]), reverse=True)[:6]
    return result[:6]


def compute_iv_skew(chain, spot, distance=250):
    """
    IV skew = PE IV minus CE IV at equal distances from spot.
    Positive = puts more expensive = fear premium = good to sell puts.
    """
    step      = 50
    ce_strike = round((spot + distance) / step) * step
    pe_strike = round((spot - distance) / step) * step
    chain_map = {r["strike"]: r for r in chain}
    ce_row    = chain_map.get(ce_strike, {})
    pe_row    = chain_map.get(pe_strike, {})

    ce_iv = float(ce_row.get("ce_iv", 0))
    pe_iv = float(pe_row.get("pe_iv", 0))
    skew  = round(pe_iv - ce_iv, 2) if ce_iv > 0 and pe_iv > 0 else 0.0

    if skew >= 3.0:
        label = f"+{skew:.1f} HIGH PUT SKEW -- PE overpriced, good to sell"
        color = "green"
    elif skew >= 1.5:
        label = f"+{skew:.1f} Moderate put skew"
        color = "green"
    elif skew >= -1.0:
        label = f"{skew:+.1f} Neutral"
        color = "white"
    else:
        label = f"{skew:.1f} Call skew"
        color = "yellow"

    return {
        "ce_strike": ce_strike, "ce_iv": ce_iv,
        "pe_strike": pe_strike, "pe_iv": pe_iv,
        "skew":      skew,
        "label":     label,
        "color":     color,
    }

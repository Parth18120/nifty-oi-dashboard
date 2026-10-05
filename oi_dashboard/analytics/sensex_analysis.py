"""
sensex_analysis.py
Fair-value + OVR% computation for Sensex option chain.
Uses real Sensex realized vol (sensex_volatility.py) — not a guessed multiplier.
Field names deliberately match Nifty chain rows so the toggle table
can reuse the same renderer without branching on source.
"""
import math
from statistics import NormalDist

from config import STRUCTURAL_IV_PREMIUM_PP


def bs_price(S, K, T, r, sigma, side):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    N  = NormalDist().cdf
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if side == "CE":
        return S * N(d1) - K * math.exp(-r * T) * N(d2)
    return K * math.exp(-r * T) * N(-d2) - S * N(-d1)


# Sensex DTE-scaled sweet zone (absolute-point OTM distances).
# Derived from sensex_spot_all.csv empirical stats:
#   gap_p90 = 960pt  range_p90 = 1404pt  (124 trading days)
# DTE=0 : intraday risk only   → min ≈ range_p90 × 0.93
# DTE=1 : gap+range at p90     → min ≈ (gap+range)_p90 × 0.76
# DTE≥2 : sqrt-time scaling from DTE=1 base, capped for tradeability
# Sanity-checked: DTE=5 min (3200pt) > old static max (2500pt); correctly
# excludes 77600 CE (845pt) at DTE=5.  DTE=1 max (2700pt) spans the
# "2500-3000pt territory" the user described as working well near expiry.
_SENSEX_ZONES = {
    0: (1300, 2000),
    1: (1800, 2700),
    2: (2500, 3500),
    3: (2800, 4000),
    4: (3000, 4200),
    5: (3200, 4500),
    6: (3500, 5000),
    7: (3800, 5500),
}


def compute_sensex_overpricing(sensex_data, nifty_sigma_fallback=0.12):
    """
    Compute OVR% for each Sensex strike using REAL Sensex RV.
    Falls back to nifty_sigma_fallback × 1.4 only if CSV is unavailable.

    Field naming: uses ce_rv_realistic_fair / ce_rv_realistic_ovr to match
    Nifty chain rows so the Premium Valuation toggle can use one renderer.
    """
    if not sensex_data:
        return None

    # ── Real Sensex sigma ─────────────────────────────────────────────────────
    try:
        from oi_dashboard.analytics.sensex_volatility import get_sensex_sigma
        real_rv, rv_source = get_sensex_sigma()
    except Exception as _e:
        print(f"[SENSEX] volatility import failed: {_e}")
        real_rv, rv_source = None, "import_error"

    if real_rv is None or real_rv <= 0:
        print(f"[SENSEX] no real history — emergency fallback (x1.4 Nifty proxy)")
        real_rv   = nifty_sigma_fallback * 1.4
        rv_source = "fallback_proxy"

    expected_iv = real_rv + STRUCTURAL_IV_PREMIUM_PP

    spot        = sensex_data["spot"]
    dte         = sensex_data["dte"]           # integer — for display/zones only
    pricing_dte = sensex_data.get("pricing_dte", max(dte / 365, 0.001) * 365)
    rows        = sensex_data["chain_rows"]
    T           = pricing_dte / 365            # fractional years for Black-Scholes
    r           = 0.065

    print(f'[SENSEX BS CALC] expiry={sensex_data["expiry"]} pricing_dte={pricing_dte:.4f}d T={T:.6f}yr sigma={expected_iv*100:.2f}%')
    print(f'[SENSEX CHAIN RAW] total strikes from Dhan: {len(rows)}')

    enriched = []
    for row in rows:
        er = dict(row)
        s  = row["strike"]

        ce_itm = s < spot
        pe_itm = s > spot

        for side in ["ce", "pe"]:
            ltp  = row[f"{side}_ltp"]
            fair = bs_price(spot, s, T, r, expected_iv, side.upper())

            if side == "ce" and 78000 <= s <= 78800:
                passed = (not ce_itm) and fair > 0.10 and ltp > 0.10
                print(f'[SENSEX FAIR CHECK] strike={s:.0f} ce_ltp={ltp:.2f} '
                      f'raw_fair={fair:.4f} threshold=0.10 passed={passed}')

            if s in (79600, 79500, 79400, 74300):
                print(f'[SENSEX FAIR DIAG] strike={s:.0f} side={side} '
                      f'spot={spot} T={T:.6f} sigma={expected_iv:.4f} '
                      f'raw_fair={fair:.4f} ltp={ltp}')

            if not (side == "ce" and ce_itm) and not (side == "pe" and pe_itm) \
               and fair > 0.10 and ltp > 0.10:
                ovr = (ltp - fair) / fair * 100
                er[f"{side}_rv_realistic_fair"] = round(fair, 2)
                er[f"{side}_rv_realistic_ovr"]  = round(ovr, 1)
            else:
                er[f"{side}_rv_realistic_fair"] = None
                er[f"{side}_rv_realistic_ovr"]  = None

        enriched.append(er)

    print(f'[SENSEX CHAIN ENRICHED] rows after enrichment: {len(enriched)}')
    if enriched:
        strikes = [r["strike"] for r in enriched]
        print(f'[SENSEX CHAIN RANGE] min_strike={min(strikes):.0f}, max_strike={max(strikes):.0f}')
        # spot diagnostic: show fair value for 77900 CE if present
        for r in enriched:
            if int(r["strike"]) == 77900:
                print(f'[SENSEX DIAG 77900CE] ltp={r.get("ce_ltp",0):.2f} fair={r.get("ce_rv_realistic_fair")} ovr={r.get("ce_rv_realistic_ovr")}')
                break

    return {
        "spot":         spot,
        "expiry":       sensex_data["expiry"],
        "dte":          dte,
        "sigma_used":   round(expected_iv * 100, 1),   # percentage, e.g. 17.6
        "sigma_source": rv_source,
        "rows":         enriched,
    }


def get_top_overpriced(enriched, top_n=6):
    """Return top N overpriced CE and PE strikes within sweet zone."""
    if not enriched:
        return {"ce_top": [], "pe_top": []}

    spot = enriched["spot"]
    rows = enriched["rows"]
    dte  = enriched["dte"]
    zone_range = _SENSEX_ZONES.get(min(dte, 7), (1800, 2700))
    min_otm, max_otm = zone_range
    print(f'[SENSEX ZONE LOOKUP] dte_used_for_zone_lookup={dte}')
    print(f'[SENSEX ZONE LOOKUP] zone_range_selected={zone_range}')
    print(f'[SENSEX ZONE TABLE FULL] {_SENSEX_ZONES}')

    ce_cand, pe_cand = [], []

    for row in rows:
        distance = abs(row["strike"] - spot)
        if not (min_otm <= distance <= max_otm):
            continue

        if row["strike"] > spot:
            ovr = row.get("ce_rv_realistic_ovr")
            ltp = row.get("ce_ltp", 0)
            if ovr is not None and ovr > 5 and ltp >= 2:
                ce_cand.append({
                    "strike":   int(row["strike"]),
                    "distance": int(distance),
                    "ltp":      ltp,
                    "fair":     row["ce_rv_realistic_fair"],
                    "ovr":      ovr,
                    "iv":       row.get("ce_iv", 0),
                    "oi":       row.get("ce_oi", 0),
                })

        if row["strike"] < spot:
            ovr = row.get("pe_rv_realistic_ovr")
            ltp = row.get("pe_ltp", 0)
            if ovr is not None and ovr > 5 and ltp >= 2:
                pe_cand.append({
                    "strike":   int(row["strike"]),
                    "distance": int(distance),
                    "ltp":      ltp,
                    "fair":     row["pe_rv_realistic_fair"],
                    "ovr":      ovr,
                    "iv":       row.get("pe_iv", 0),
                    "oi":       row.get("pe_oi", 0),
                })

    ce_cand.sort(key=lambda x: x["ovr"], reverse=True)
    pe_cand.sort(key=lambda x: x["ovr"], reverse=True)

    return {"ce_top": ce_cand[:top_n], "pe_top": pe_cand[:top_n]}

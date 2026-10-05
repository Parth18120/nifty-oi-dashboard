"""Synthetic option-chain snapshot used by demo mode when the market is closed."""

from oi_dashboard.analytics.pricing import bs_fair_price, overpricing_pct


def get_mock_data():
    import random
    spot = 24200.0
    atm  = 24200
    strikes = list(range(atm - 500, atm + 550, 50))
    dte  = 5
    mock_rv = 0.125   # 12.5% RV for demo
    chain = []
    for s in strikes:
        dist = s - spot
        ce_iv  = 13.5 + abs(dist) * 0.003 + random.uniform(-0.3, 0.3)
        pe_iv  = 14.2 + abs(dist) * 0.004 + random.uniform(-0.3, 0.3)
        ce_ltp = max(0.5, (spot - s) * -1 * 0.01 + ce_iv * 0.8 + random.uniform(-1, 1)) if s > spot else max(0.5, (spot - s) * 0.4 + ce_iv * 1.2)
        pe_ltp = max(0.5, (s - spot) * 0.4 + pe_iv * 1.2) if s < spot else max(0.5, pe_iv * 0.8 + random.uniform(-1, 1))
        ce_oi  = max(10000, int((600000 - abs(dist) * 800) * random.uniform(0.8, 1.2)))
        pe_oi  = max(10000, int((700000 - abs(dist) * 750) * random.uniform(0.8, 1.2)))
        ce_oi_chg = int(random.uniform(-50000, 80000))
        pe_oi_chg = int(random.uniform(-40000, 90000))
        ce_rv_fair, _ = bs_fair_price(spot, s, mock_rv, dte, "CE")
        pe_rv_fair, _ = bs_fair_price(spot, s, mock_rv, dte, "PE")
        ce_rv_ovr = overpricing_pct(ce_ltp, ce_rv_fair)
        pe_rv_ovr = overpricing_pct(pe_ltp, pe_rv_fair)
        _mock_exp_iv = mock_rv + 0.03
        ce_rv_realistic_fair, _ = bs_fair_price(spot, s, _mock_exp_iv, dte, "CE")
        pe_rv_realistic_fair, _ = bs_fair_price(spot, s, _mock_exp_iv, dte, "PE")
        ce_rv_realistic_ovr = overpricing_pct(ce_ltp, ce_rv_realistic_fair)
        pe_rv_realistic_ovr = overpricing_pct(pe_ltp, pe_rv_realistic_fair)
        chain.append({
            "strike": s,
            "ce_ltp": round(ce_ltp, 2),
            "ce_iv": round(ce_iv, 2),
            "ce_oi": ce_oi,
            "ce_oi_chg": ce_oi_chg,
            "ce_oi_prev": ce_oi - ce_oi_chg,
            "ce_volume": int(ce_oi * random.uniform(0.1, 0.4)),
            "ce_bid": round(ce_ltp - 0.5, 2),
            "ce_ask": round(ce_ltp + 0.5, 2),
            "pe_ltp": round(pe_ltp, 2),
            "pe_iv": round(pe_iv, 2),
            "pe_oi": pe_oi,
            "pe_oi_chg": pe_oi_chg,
            "pe_oi_prev": pe_oi - pe_oi_chg,
            "pe_volume": int(pe_oi * random.uniform(0.1, 0.4)),
            "pe_bid": round(pe_ltp - 0.5, 2),
            "pe_ask": round(pe_ltp + 0.5, 2),
            "ce_chg_from_open": ce_oi_chg,
            "pe_chg_from_open": pe_oi_chg,
            "net_gamma_exp": round(random.uniform(-0.00001, 0.00001), 8),
            "ce_theo_price": round(ce_ltp * 0.97, 2),
            "pe_theo_price": round(pe_ltp * 0.97, 2),
            "ce_overpriced_pct": round(random.uniform(-5, 20), 1),
            "pe_overpriced_pct": round(random.uniform(-5, 20), 1),
            "ce_rv_fair":            round(ce_rv_fair, 2),
            "pe_rv_fair":            round(pe_rv_fair, 2),
            "ce_rv_ovr":             ce_rv_ovr,
            "pe_rv_ovr":             pe_rv_ovr,
            "ce_rv_ovr_chg":         round(random.uniform(-5, 5), 1),
            "pe_rv_ovr_chg":         round(random.uniform(-5, 5), 1),
            "ce_rv_realistic_fair":  round(ce_rv_realistic_fair, 2),
            "pe_rv_realistic_fair":  round(pe_rv_realistic_fair, 2),
            "ce_rv_realistic_ovr":   ce_rv_realistic_ovr,
            "pe_rv_realistic_ovr":   pe_rv_realistic_ovr,
            "rv20":          round(mock_rv * 100, 2),
        })

    total_ce_oi = sum(r["ce_oi"] for r in chain)
    total_pe_oi = sum(r["pe_oi"] for r in chain)
    pcr = round(total_pe_oi / total_ce_oi, 3)
    put_wall  = max(chain, key=lambda r: r["pe_oi"])["strike"]
    call_wall = max(chain, key=lambda r: r["ce_oi"])["strike"]
    atm_row   = next((r for r in chain if r["strike"] == atm), chain[10])
    straddle  = round(atm_row["ce_ltp"] + atm_row["pe_ltp"], 2)

    pcr_history = [
        {"time": "09:30:00", "pcr": 0.91},
        {"time": "10:00:00", "pcr": 0.95},
        {"time": "10:30:00", "pcr": 1.02},
        {"time": "11:00:00", "pcr": 1.08},
        {"time": "11:30:00", "pcr": 1.15},
        {"time": "12:00:00", "pcr": pcr},
    ]

    oi_history = {}
    for r in chain:
        oi_history[r["strike"]] = [{"time": "12:00:00", "ce_oi": r["ce_oi"], "pe_oi": r["pe_oi"], "ce_ltp": r["ce_ltp"], "pe_ltp": r["pe_ltp"]}]

    support_rows    = sorted(chain, key=lambda r: r["pe_oi"], reverse=True)[:3]
    resistance_rows = sorted(chain, key=lambda r: r["ce_oi"], reverse=True)[:3]
    support    = [r["strike"] for r in support_rows]
    resistance = [r["strike"] for r in resistance_rows]

    ce_skew_strike = 24450
    pe_skew_strike = 23950
    ce_skew_row = next((r for r in chain if r["strike"] == ce_skew_strike), chain[15])
    pe_skew_row = next((r for r in chain if r["strike"] == pe_skew_strike), chain[5])
    skew_val = round(pe_skew_row["pe_iv"] - ce_skew_row["ce_iv"], 2)

    from datetime import date, timedelta
    today = date.today()
    days_ahead = (1 - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    next_tuesday = today + timedelta(days=days_ahead)

    return {
        "timestamp":      "12:00:00",
        "market_open":    False,
        "demo_mode":      True,
        "spot": {
            "ltp": spot, "open": 24050.0, "high": 24310.0,
            "low": 24010.0, "prev_close": 24150.0,
            "change": 50.0, "change_pct": 0.21,
        },
        "vix":            14.8,
        "chain":          chain,
        "dte":            dte,
        "expiry":         next_tuesday.strftime("%Y-%m-%d"),
        "pcr":            pcr,
        "pcr_label":      "MILD BEARISH BIAS",
        "pcr_color":      "yellow",
        "max_pain":       24150,
        "support":        support,
        "resistance":     resistance,
        "oi_alerts": [
            {"strike": 24000, "side": "PE", "change": 250000, "type": "BUILDUP", "note": "PE 24000 +2.5L -- support building"},
            {"strike": 24500, "side": "CE", "change": 180000, "type": "BUILDUP", "note": "CE 24500 +1.8L -- resistance building"},
            {"strike": 23800, "side": "PE", "change": -120000, "type": "UNWIND",  "note": "PE 23800 -1.2L -- support weakening"},
        ],
        "iv_skew": {
            "ce_strike": ce_skew_strike, "ce_iv": ce_skew_row["ce_iv"],
            "pe_strike": pe_skew_strike, "pe_iv": pe_skew_row["pe_iv"],
            "skew": skew_val,
            "label": f"+{skew_val} Moderate put skew" if skew_val > 0 else f"{skew_val} Neutral",
            "color": "green" if skew_val > 1 else "white",
        },
        "pos_greeks": {
            "breakdown": [
                {"type": "SELL", "option": "CE", "strike": 24500, "entry_price": 62.0,
                 "current_ltp": 48.5, "iv": 13.8, "delta": -0.18, "gamma": 0.0003,
                 "theta": 45.2, "vega": -38.4, "unrealised_pnl": 1012.50},
                {"type": "SELL", "option": "PE", "strike": 23900, "entry_price": 58.0,
                 "current_ltp": 44.2, "iv": 14.6, "delta": 0.16, "gamma": 0.0003,
                 "theta": 42.8, "vega": -36.1, "unrealised_pnl": 1035.00},
            ],
            "net_delta": -0.02,
            "net_gamma": 0.0006,
            "net_theta": 88.0,
            "net_vega": -74.5,
        },
        "oi_history":     oi_history,
        "pcr_history":    pcr_history,
        "my_positions": [
            {"type": "SELL", "option": "CE", "strike": 24500, "entry_price": 62.0, "qty": 1},
            {"type": "SELL", "option": "PE", "strike": 23900, "entry_price": 58.0, "qty": 1},
        ],
        "oi_centre":      round(sum(r["strike"] * (r["ce_oi"] + r["pe_oi"]) for r in chain) / sum(r["ce_oi"] + r["pe_oi"] for r in chain), 2),
        "put_wall":       put_wall,
        "call_wall":      call_wall,
        "straddle_price": straddle,
        "total_ce_oi":    total_ce_oi,
        "total_pe_oi":    total_pe_oi,
        "rv20":             12.5,
        "rv20_estimated":   False,
        "paper_trades":     [
            {"id":1,"strike":24500,"option_type":"CE","entry_time":"09:32:00",
             "entry_ltp":58.0,"entry_fair":38.0,"entry_ovr":52.6,"entry_spot":24200.0,
             "stop_price":116.0,"target_price":23.2,"current_ltp":44.5,
             "current_ovr":35.2,"current_spot":24210.0,"max_ltp":60.0,"min_ltp":40.0,
             "max_adverse_spot":15.0,"status":"OPEN","exit_time":None,"exit_ltp":None,
             "exit_reason":None,"pnl_unit":13.5,"pnl_lot":1012.5,"updates":[]},
            {"id":2,"strike":23900,"option_type":"PE","entry_time":"09:45:00",
             "entry_ltp":55.0,"entry_fair":34.0,"entry_ovr":61.8,"entry_spot":24200.0,
             "stop_price":110.0,"target_price":22.0,"current_ltp":22.0,
             "current_ovr":18.1,"current_spot":24210.0,"max_ltp":57.0,"min_ltp":22.0,
             "max_adverse_spot":0.0,"status":"TARGET_HIT","exit_time":"11:02:00",
             "exit_ltp":22.0,"exit_reason":"PROFIT_60PCT","pnl_unit":33.0,"pnl_lot":2475.0,
             "updates":[]},
        ],
        "paper_trade_date": __import__("datetime").date.today().strftime("%Y-%m-%d"),
        "refresh_interval": 60,
    }

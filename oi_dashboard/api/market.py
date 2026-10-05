"""Market-data API: dashboard payload, refresh, demo mode, expiries and diagnostics."""

import time

from flask import Blueprint, jsonify, send_from_directory

from oi_dashboard.analytics.intraday import build_spike_api, build_spike_log_api
from oi_dashboard.core import state
from oi_dashboard.core.paths import WEB_DIR
from oi_dashboard.market.mock_data import get_mock_data
from oi_dashboard.market.snapshot import get_fresh_data

bp = Blueprint("market", __name__)


@bp.route("/api/ping")
def api_ping():
    return jsonify({"status": "ok", "port": 5050})


@bp.route("/api/debug")
def api_debug():
    result = {}
    try:
        from config import ACCESS_TOKEN, CLIENT_ID
        result["client_id"]    = CLIENT_ID
        result["token_prefix"] = ACCESS_TOKEN[:30] + "..."
    except Exception as e:
        result["config_error"] = str(e)
        return jsonify(result)

    import requests
    headers = {
        "access-token": ACCESS_TOKEN,
        "client-id":    CLIENT_ID,
        "Content-type": "application/json",
        "Accept":       "application/json",
    }

    # Test all spot endpoints
    endpoints = [
        ("quote",  "https://api.dhan.co/v2/marketfeed/quote",  {"NSE_EQ": ["1333"]}),
        ("ohlc",   "https://api.dhan.co/v2/marketfeed/ohlc",   {"NSE_EQ": ["1333"]}),
        ("ltp_v1", "https://api.dhan.co/v2/marketfeed/ltp",    {"NSE_EQ": ["1333"]}),
        ("ltp_v2", "https://api.dhan.co/v2/marketfeed/ltp",    {"securities": {"NSE_EQ": ["1333"]}}),
    ]
    for name, url, payload in endpoints:
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=8)
            result[f"{name}_status"] = r.status_code
            result[f"{name}_data"]   = r.json()
        except Exception as e:
            result[f"{name}_error"] = str(e)
        time.sleep(1.5)   # avoid triggering rate limiting from this debug call itself

    # Show all available dhanhq methods and test quote_data
    try:
        import time as _time

        from dhanhq import DhanContext, dhanhq
        dhan = dhanhq(DhanContext(CLIENT_ID, ACCESS_TOKEN))
        methods = [m for m in dir(dhan) if not m.startswith("_")]
        result["dhanhq_methods"] = methods
        _time.sleep(1)
        spot_test = dhan.quote_data({"NSE_EQ": [1333]})
        result["quote_data_spot"] = spot_test
        _time.sleep(1)
        vix_test = dhan.quote_data({"IDX_I": [21]})
        result["quote_data_vix"] = vix_test
        _time.sleep(1)
        ticker_test = dhan.ticker_data({"NSE_EQ": [1333]})
        result["ticker_data_spot"] = ticker_test
    except Exception as e:
        result["dhanhq_error"] = str(e)

    return jsonify(result)


@bp.route("/")
@bp.route("/dashboard.html")
def serve_dashboard():
    return send_from_directory(str(WEB_DIR), "dashboard.html")


@bp.route("/api/data")
def api_data():
    return jsonify(get_fresh_data())


@bp.route("/api/refresh")
def api_refresh():
    state.cache_time = 0   # force fresh fetch
    return jsonify(get_fresh_data())


@bp.route("/api/demo/on")
def demo_on():
    state.demo_mode = True
    return jsonify({"demo_mode": True})


@bp.route("/api/demo/off")
def demo_off():
    state.demo_mode = False
    return jsonify({"demo_mode": False})


@bp.route("/api/expiries")
def api_expiries():
    try:
        from oi_dashboard.feeds import dhan_feed as F
        expiries = F.fetch_expiry_dates()
        return jsonify({"expiries": expiries})
    except Exception as e:
        return jsonify({"error": str(e), "expiries": []})


@bp.route("/api/chain/<expiry_date>")
def api_chain_expiry(expiry_date):
    if state.demo_mode:
        mock = get_mock_data()
        return jsonify({
            "expiry": expiry_date,
            "dte": mock["dte"],
            "spot": mock["spot"]["ltp"],
            "chain": mock["chain"],
        })
    try:
        from datetime import date as _date
        from datetime import datetime as _dt

        from config import DIVIDEND_YIELD, RISK_FREE_RATE

        from oi_dashboard.analytics import greeks as G
        from oi_dashboard.feeds import dhan_feed as F

        spot_data = F.fetch_nifty_spot()
        spot = spot_data["ltp"]
        chain = F.fetch_option_chain_for_expiry(spot, expiry_date)

        today = _date.today()
        try:
            exp_date = _dt.strptime(expiry_date, "%Y-%m-%d").date()
            dte = max(0, (exp_date - today).days)
        except Exception:
            dte = 3

        chain_out = []
        for row in chain:
            s = row["strike"]
            ce_g = G.compute_greeks(spot, s, row["ce_iv"] or 14.0, max(dte, 1), "CE",
                                    RISK_FREE_RATE, DIVIDEND_YIELD)
            pe_g = G.compute_greeks(spot, s, row["pe_iv"] or 14.0, max(dte, 1), "PE",
                                    RISK_FREE_RATE, DIVIDEND_YIELD)
            net_gamma = ce_g["gamma"] * row["ce_oi"] - pe_g["gamma"] * row["pe_oi"]
            ce_theo = ce_g["price"]
            pe_theo = pe_g["price"]
            chain_out.append({**row,
                "ce_chg_from_open": 0,
                "pe_chg_from_open": 0,
                "net_gamma_exp":    round(net_gamma, 8),
                "ce_theo_price":    round(ce_theo, 2),
                "pe_theo_price":    round(pe_theo, 2),
                "ce_overpriced_pct": round(((row["ce_ltp"] - ce_theo) / ce_theo * 100) if ce_theo > 0 else 0, 1),
                "pe_overpriced_pct": round(((row["pe_ltp"] - pe_theo) / pe_theo * 100) if pe_theo > 0 else 0, 1),
            })

        return jsonify({
            "expiry": expiry_date,
            "dte":    dte,
            "spot":   spot,
            "chain":  chain_out,
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "traceback": traceback.format_exc(), "chain": []})


@bp.route("/api/pcr_history")
def api_pcr_history():
    from oi_dashboard.analytics import chain_analysis as _CA
    return jsonify({"history": _CA.get_pcr_history()})


@bp.route("/api/spike_log")
def api_spike_log():
    try:
        from oi_dashboard.analytics import spike_detector as _sd
        log   = build_spike_log_api(limit=20)
        stats = _sd.get_spike_stats()
        return jsonify({"log": log, "stats": stats, "active": build_spike_api()})
    except Exception as e:
        return jsonify({"error": str(e), "log": [], "stats": None, "active": None})


@bp.route("/api/iv_crush_log")
def api_iv_crush_log():
    return jsonify({"log": state.iv_crush_log})

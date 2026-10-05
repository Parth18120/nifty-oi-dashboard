"""Backtest and historical-data API."""

from datetime import datetime

from flask import Blueprint, jsonify, request, send_file

from oi_dashboard.core import state
from oi_dashboard.core.constants import IST

bp = Blueprint("backtest", __name__)


@bp.route("/api/backtest_exits")
def api_backtest_exits():
    """
    Replays today's closed trades and compares exit reason under old logic
    (fixed 60% decay target) vs new logic (DTE-scaled target + trailing stop).
    Returns a list of diffs for audit.
    """
    try:
        from config import (
            TARGET_DECAY_BY_DTE,
            TARGET_DECAY_DEFAULT,
            TRAILING_ACTIVATION_DECAY,
            TRAILING_GIVEBACK_PCT,
        )
    except Exception:
        TARGET_DECAY_BY_DTE      = {7:0.30,6:0.35,5:0.40,4:0.50,3:0.55,2:0.60,1:0.65}
        TARGET_DECAY_DEFAULT     = 0.40
        TRAILING_ACTIVATION_DECAY = 0.25
        TRAILING_GIVEBACK_PCT     = 0.30

    results = []
    for t in state.paper_trades:
        if t.get("status") == "OPEN":
            continue
        entry   = t.get("net_credit_entry", 0) or 0
        exit_net = t.get("exit_net") or t.get("net_credit_current") or 0
        dte      = int(t.get("dte_at_entry") or 5)
        old_target = entry * 0.40   # fixed legacy
        new_decay  = TARGET_DECAY_BY_DTE.get(min(dte, 7), TARGET_DECAY_DEFAULT)
        new_target = entry * (1 - new_decay)

        trail_activation = entry * (1 - TRAILING_ACTIVATION_DECAY)

        old_exit = t.get("exit_reason", "--")
        new_exit = old_exit  # unchanged unless target differs

        if exit_net <= new_target and exit_net > old_target:
            new_exit = f"TARGET (new DTE={dte} {new_decay*100:.0f}% decay)"
        elif exit_net > new_target and exit_net <= old_target:
            new_exit = f"NO_EXIT (new target ₹{new_target:.2f} not reached yet)"

        results.append({
            "id":            t.get("id"),
            "trade_type":    t.get("trade_type"),
            "dte":           dte,
            "entry_credit":  entry,
            "exit_net":      exit_net,
            "old_target":    round(old_target, 2),
            "new_target":    round(new_target, 2),
            "trail_trigger": round(trail_activation, 2),
            "old_exit":      old_exit,
            "new_exit":      new_exit,
            "changed":       old_exit != new_exit,
        })

    changed = [r for r in results if r["changed"]]
    return jsonify({"total": len(results), "changed": len(changed), "trades": results})


@bp.route("/api/backtest/run")
def api_backtest_run():
    """GET /api/backtest/run?days=90 — simulate model on historical OHLC."""
    try:
        from oi_dashboard.trading import backtest as BT
        days = max(30, min(int(request.args.get("days", 90)), 365))
        lots = max(1,  min(int(request.args.get("lots", 1)), 50))
        cached = BT.get_cached_backtest(days, lots)
        if cached:
            return jsonify({"status": "ok", "cached": True, "result": cached})
        from oi_dashboard.storage import db as _bt_db
        rows   = _bt_db.get_spot_daily(days + 25)
        result = BT.run_backtest(rows, {"lots": lots})
        BT.set_cached_backtest(days, result, lots)
        return jsonify({"status": "ok", "cached": False, "result": result})
    except Exception as e:
        import traceback as _tb
        return jsonify({"status": "error", "error": str(e),
                        "traceback": _tb.format_exc()}), 500


@bp.route("/api/backtest/sweep")
def api_backtest_sweep():
    """GET /api/backtest/sweep — parameter sweep, returns top 15 by OOS Sharpe."""
    try:
        from oi_dashboard.storage import db as _bt_db
        from oi_dashboard.trading import backtest as BT
        days = max(60, min(int(request.args.get("days", 120)), 365))
        rows = _bt_db.get_spot_daily(days + 25)
        results = BT.parameter_sweep(rows)
        return jsonify({"status": "ok", "results": results[:15],
                        "total_combos": len(results)})
    except Exception as e:
        import traceback as _tb
        return jsonify({"status": "error", "error": str(e),
                        "traceback": _tb.format_exc()}), 500


@bp.route("/api/backtest/export")
def api_backtest_export():
    """GET /api/backtest/export?days=90 — run backtest and return Excel file."""
    try:
        import tempfile

        from oi_dashboard.storage import db as _bt_db
        from oi_dashboard.trading import backtest as BT
        days   = max(30, min(int(request.args.get("days", 90)), 365))
        rows   = _bt_db.get_spot_daily(days + 25)
        result = BT.run_backtest(rows)
        tmp    = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        tmp.close()
        BT._export_backtest_excel(result, tmp.name)
        return send_file(tmp.name, as_attachment=True,
                         download_name=f"backtest_{days}d.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@bp.route("/api/chain/history")
def api_chain_history():
    """GET /api/chain/history?date=YYYY-MM-DD&strike=24250&type=CE"""
    from oi_dashboard.storage import db as _db_ch
    date_str  = request.args.get("date", datetime.now(IST).strftime("%Y-%m-%d"))
    strike    = request.args.get("strike", type=int)
    opt_type  = request.args.get("type", "CE").upper()
    if not strike:
        return jsonify({"error": "strike param required"}), 400
    rows = _db_ch.get_option_chain_history(date_str, strike, opt_type)
    return jsonify({"date": date_str, "strike": strike, "type": opt_type, "rows": rows})


@bp.route("/api/backtest/real")
def api_backtest_real():
    """GET /api/backtest/real?from=YYYY-MM-DD&to=YYYY-MM-DD"""
    from oi_dashboard.trading import backtest as BT
    from_date = request.args.get("from")
    to_date   = request.args.get("to")
    try:
        result = BT.run_real_backtest(from_date, to_date)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/backtest/eod_stats")
def api_backtest_eod_stats():
    """Return count of option_eod_history rows available."""
    from oi_dashboard.storage import db as _eod_db
    stats = _eod_db.get_eod_dates_available("NIFTY")
    return jsonify(stats)

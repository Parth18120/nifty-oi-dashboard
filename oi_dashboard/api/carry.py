"""Carry-forward prompt API (end-of-day hold/close decisions)."""

from datetime import datetime

from flask import Blueprint, jsonify, request

from oi_dashboard.core import state
from oi_dashboard.core.constants import IST, LOT_SIZE

bp = Blueprint("carry", __name__)


@bp.route("/api/carry_prompt")
def api_carry_prompt():
    open_trades = [t for t in state.paper_trades if t.get("status") == "OPEN"]
    trades_out = []
    for t in open_trades:
        lots = t.get("lots", 1)
        credit = t.get("net_credit_entry") or t.get("entry_ltp") or 0
        theta_est = round(credit * 0.03 * lots * LOT_SIZE, 0)  # ~3% daily theta decay estimate
        trades_out.append({
            "trade_type":  t.get("trade_type"),
            "sell_strike": t.get("sell_strike") or t.get("strike"),
            "buy_strike":  t.get("buy_strike"),
            "side":        t.get("side") or t.get("option_type"),
            "lots":        lots,
            "pnl_total":   t.get("pnl_total", 0),
            "credit":      credit,
            "theta_est":   theta_est,
        })
    p90_gap = state.gap_stats.get("p90", 0) if state.gap_stats else 0
    verdict = "CARRY" if (p90_gap < 0.5 and len(open_trades) > 0) else "EXIT"
    return jsonify({
        "pending":     state.carry_prompt_pending,
        "trades":      trades_out,
        "gap_stats":   state.gap_stats,
        "p90_gap_pct": p90_gap,
        "verdict":     verdict,
    })


@bp.route("/api/carry_prompt/force", methods=["POST"])
def api_carry_prompt_force():
    state.carry_prompt_pending = True
    return jsonify({"status": "ok", "pending": True})


@bp.route("/api/carry_prompt/action", methods=["POST"])
def api_carry_prompt_action():
    data   = request.get_json(silent=True) or {}
    action = data.get("action", "skip").upper()  # CARRY, EXIT, SKIP
    state.carry_prompt_pending = False
    if action == "CARRY":
        for t in state.paper_trades:
            if t.get("status") == "OPEN":
                t["carry_forward"] = True
                t["entry_reason"]  = "carry_forward"
        return jsonify({"status": "ok", "action": "CARRY", "affected": len([t for t in state.paper_trades if t.get("carry_forward")])})
    elif action == "EXIT":
        now_ist  = datetime.now(IST)
        time_str = now_ist.strftime("%H:%M:%S")
        affected = 0
        for t in state.paper_trades:
            if t.get("status") == "OPEN":
                lots   = t.get("lots", 1)
                credit = t.get("net_credit_entry") or t.get("entry_ltp") or 0
                cur    = t.get("net_credit_current") or credit
                pnl_u  = round((credit - cur) * LOT_SIZE, 2)
                t.update(status="EOD", exit_time=time_str, exit_reason="CARRY_EXIT",
                         pnl_per_lot=pnl_u, pnl_total=round(pnl_u * lots, 2))
                affected += 1
        return jsonify({"status": "ok", "action": "EXIT", "affected": affected})
    else:
        return jsonify({"status": "ok", "action": "SKIP"})

"""Trade journal API."""

from flask import Blueprint, jsonify, request

from oi_dashboard.core import state

bp = Blueprint("journal", __name__)


@bp.route("/api/journal", methods=["GET"])
def api_journal_get():
    return jsonify({"trades": list(reversed(state.journal_trades[-100:]))})


@bp.route("/api/journal", methods=["POST"])
def api_journal_post():
    data = request.get_json(silent=True) or {}
    try:
        entry = float(data.get("entry") or 0)
        exit_ = float(data.get("exit")  or 0) if data.get("exit") else None
        lots  = int(data.get("lots") or 1)
        pnl   = round((entry - exit_) * lots * 50, 2) if exit_ and data.get("dir","S") == "S" else (round((exit_ - entry) * lots * 50, 2) if exit_ else None)
        state.journal_id_counter += 1
        record = {
            "id":     state.journal_id_counter,
            "date":   data.get("date", ""),
            "strike": data.get("strike", ""),
            "type":   data.get("type", "CE"),
            "dir":    data.get("dir", "S"),
            "entry":  entry,
            "exit":   exit_,
            "lots":   lots,
            "carry":  bool(int(data.get("carry", 0))),
            "pnl":    pnl,
        }
        state.journal_trades.append(record)
        return jsonify({"ok": True, "message": f"Logged #{state.journal_id_counter}: {record['strike']} {record['type']} @ ₹{entry}", "trade": record})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 400

"""Panic-drop detection and the long-option bounce trade."""

from datetime import datetime, timedelta

from oi_dashboard.core import state
from oi_dashboard.core.constants import IST, LOT_SIZE
from oi_dashboard.storage.session_store import save_paper_trades


def detect_panic_and_buy(chain_out, spot, dte, time_str):
    """Detects 150pt+ 10-min panic moves + 100%+ premium spike, enters long option."""

    now_ist   = datetime.now(IST)
    today_str = now_ist.strftime("%Y-%m-%d")
    mkt_min   = now_ist.hour * 60 + now_ist.minute

    if state.panic_buy_date != today_str:
        state.panic_buy_date   = today_str
        state.panic_buy_active = False

    if state.panic_buy_active:            return
    if dte <= 1:                     return
    if mkt_min < 9 * 60 + 30:       return
    if mkt_min > 14 * 60:           return
    if len(state.spot_10min_history) < 3: return

    ten_min_ago = now_ist - timedelta(minutes=10)
    recent = [(t, s) for t, s in state.spot_10min_history if t >= ten_min_ago]
    if len(recent) < 2:
        return

    oldest_spot = recent[0][1]
    move_pts    = spot - oldest_spot
    abs_move    = abs(move_pts)
    if abs_move < 150:
        return

    going_down = move_pts < -150
    going_up   = move_pts > 150
    print(f"[PANIC] Spot moved {move_pts:+.0f}pt in 10min — checking premium spike")

    target_strike = target_ltp = target_type = None
    spike_pct = 0.0

    if going_down:
        candidates = []
        for row in chain_out:
            s      = row["strike"]
            otm    = spot - s
            pe_ltp = float(row.get("pe_ltp") or 0)
            if 50 <= otm <= 150 and 15 <= pe_ltp <= 80:
                low_key  = f"{s}_pe_ltp"
                sess_low = state.strike_session_lows.get(low_key, pe_ltp)
                if sess_low > 0:
                    spk = (pe_ltp - sess_low) / sess_low * 100
                    if spk >= 100:
                        candidates.append((s, pe_ltp, spk, otm))
        if candidates:
            candidates.sort(key=lambda x: x[2], reverse=True)
            target_strike, target_ltp, spike_pct = candidates[0][0], candidates[0][1], candidates[0][2]
            target_type = "PE"

    elif going_up:
        candidates = []
        for row in chain_out:
            s      = row["strike"]
            otm    = s - spot
            ce_ltp = float(row.get("ce_ltp") or 0)
            if 50 <= otm <= 150 and 15 <= ce_ltp <= 80:
                low_key  = f"{s}_ce_ltp"
                sess_low = state.strike_session_lows.get(low_key, ce_ltp)
                if sess_low > 0:
                    spk = (ce_ltp - sess_low) / sess_low * 100
                    if spk >= 100:
                        candidates.append((s, ce_ltp, spk, otm))
        if candidates:
            candidates.sort(key=lambda x: x[2], reverse=True)
            target_strike, target_ltp, spike_pct = candidates[0][0], candidates[0][1], candidates[0][2]
            target_type = "CE"

    if not target_strike:
        print(f"[PANIC] Move={move_pts:+.0f}pt but no qualifying strike (need LTP ₹15-80, 100%+ spike)")
        return

    pb_key = (target_strike, target_type, "PANIC")
    if pb_key in state.paper_trade_ids:
        return

    lots        = 3
    stop_ltp    = round(target_ltp * 0.65, 2)
    target_exit = round(target_ltp * 1.80, 2)

    pb = {
        "id":            len(state.paper_trades) + 1,
        "trade_type":    "PANIC_BUY",
        "strike":        target_strike,
        "option_type":   target_type,
        "entry_time":    time_str,
        "entry_ltp":     target_ltp,
        "entry_spot":    round(spot, 2),
        "current_ltp":   target_ltp,
        "lots":          lots,
        "stop_ltp":      stop_ltp,
        "target_ltp":    target_exit,
        "time_stop_min": 30,
        "spike_pct":     round(spike_pct, 1),
        "spot_move_pts": round(move_pts, 1),
        "status":        "OPEN",
        "exit_time":     None,
        "exit_ltp":      None,
        "exit_reason":   None,
        "pnl_per_lot":   0.0,
        "pnl_total":     0.0,
        "entry_reason":  "auto",
        "lot_size_used":  LOT_SIZE,
    }

    state.paper_trades.append(pb)
    state.paper_trade_ids.add(pb_key)
    state.panic_buy_active     = True
    state.panic_buy_entry_time = now_ist
    save_paper_trades()

    print(f"[PANIC BUY] ENTERED: {target_strike} {target_type} @ ₹{target_ltp:.2f} "
          f"| Spike {spike_pct:.0f}% | Move {move_pts:+.0f}pt "
          f"| Stop ₹{stop_ltp} Target ₹{target_exit} | {lots} lots")


def update_panic_buy(trade, chain_out, spot, time_str):
    """Update panic buy — check 30min time stop, stop loss, target, EOD."""

    now_ist = datetime.now(IST)
    strike  = trade["strike"]
    otype   = trade["option_type"]
    cmap    = {r["strike"]: r for r in chain_out}

    ltp_key = "pe_ltp" if otype == "PE" else "ce_ltp"
    cur_ltp = float((cmap.get(strike) or {}).get(ltp_key) or trade["current_ltp"])
    trade["current_ltp"] = round(cur_ltp, 2)

    lots     = trade.get("lots", 3)
    pnl_unit = cur_ltp - trade["entry_ltp"]
    trade["pnl_per_lot"] = round(pnl_unit * LOT_SIZE, 2)
    trade["pnl_total"]   = round(pnl_unit * LOT_SIZE * lots, 2)

    if state.panic_buy_entry_time:
        elapsed = (now_ist - state.panic_buy_entry_time).total_seconds() / 60
        if elapsed >= 30:
            trade.update(status="TIME_STOP", exit_time=time_str,
                         exit_ltp=round(cur_ltp, 2), exit_reason="TIME_STOP (30min)",
                         pnl_per_lot=round(pnl_unit * LOT_SIZE, 2),
                         pnl_total=round(pnl_unit * LOT_SIZE * lots, 2))
            state.panic_buy_active = False
            print(f"[PANIC BUY] TIME STOP: pnl=₹{trade['pnl_total']:.0f}")
            return

    if cur_ltp <= trade["stop_ltp"]:
        trade.update(status="STOPPED", exit_time=time_str,
                     exit_ltp=round(cur_ltp, 2), exit_reason="STOP (-35%)",
                     pnl_per_lot=round(pnl_unit * LOT_SIZE, 2),
                     pnl_total=round(pnl_unit * LOT_SIZE * lots, 2))
        state.panic_buy_active = False
        print(f"[PANIC BUY] STOPPED: pnl=₹{trade['pnl_total']:.0f}")
        return

    if cur_ltp >= trade["target_ltp"]:
        trade.update(status="TARGET", exit_time=time_str,
                     exit_ltp=round(cur_ltp, 2), exit_reason="TARGET (+80%)",
                     pnl_per_lot=round(pnl_unit * LOT_SIZE, 2),
                     pnl_total=round(pnl_unit * LOT_SIZE * lots, 2))
        state.panic_buy_active = False
        print(f"[PANIC BUY] TARGET: pnl=₹{trade['pnl_total']:.0f}")
        return

    if now_ist.hour == 15 and now_ist.minute >= 28:
        trade.update(status="EOD", exit_time=time_str,
                     exit_ltp=round(cur_ltp, 2), exit_reason="EOD",
                     pnl_per_lot=round(pnl_unit * LOT_SIZE, 2),
                     pnl_total=round(pnl_unit * LOT_SIZE * lots, 2))
        state.panic_buy_active = False
        print(f"[PANIC BUY] EOD: pnl=₹{trade['pnl_total']:.0f}")

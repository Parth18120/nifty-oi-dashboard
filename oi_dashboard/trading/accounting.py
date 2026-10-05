"""Trade accounting: costs, P&L, trailing stops, carry-forward and gap-reversal exits."""

from datetime import datetime

from oi_dashboard.core import state
from oi_dashboard.core.constants import IST, LOT_SIZE
from oi_dashboard.storage.session_store import save_paper_trades


def close_trade_now(trade, time_str, reason, note=""):
    """Close any trade type immediately with a given exit reason."""
    trade["status"]      = "KILLED"
    trade["exit_time"]   = time_str
    trade["exit_reason"] = reason
    if note:
        trade["exit_note"] = note
    print(f"[CLOSE] {trade.get('trade_type')} #{trade.get('id')} reason={reason} pnl=₹{trade.get('pnl_total',0):.0f}")


def compute_trade_costs(trade):
    """
    Compute and stamp realistic transaction costs onto a closed trade.
    Stores costs_total and pnl_net on the trade dict. Safe to call multiple times.
    """
    if trade.get("costs_total") is not None:
        return  # already computed
    try:
        from config import COSTS
    except Exception:
        trade["costs_total"] = 0.0
        trade["pnl_net"]     = trade.get("pnl_total", 0.0)
        return

    lots      = int(trade.get("lots", 1) or 1)
    lot_size  = int(trade.get("lot_size_used") or LOT_SIZE)
    units     = lots * lot_size

    # Number of orders: entry + exit, both legs
    t_type = trade.get("trade_type", "STRANGLE")
    if t_type == "IC":
        n_orders = 8   # 4 legs × entry + exit
    elif t_type == "GAP_SPREAD":
        n_orders = 2
    else:
        n_orders = 4   # strangle / panic: 2 legs × entry + exit

    # Sell premium (entry credit per share, i.e. per 1 unit of lot)
    sell_px  = float(trade.get("net_credit_entry", 0) or 0)
    pnl_lot  = float(trade.get("pnl_per_lot", 0) or 0)
    # buy-back premium per share at exit
    buy_px   = sell_px - (pnl_lot / lot_size) if lot_size > 0 else 0
    buy_px   = max(buy_px, 0.0)

    sell_turnover = sell_px * units
    buy_turnover  = buy_px  * units
    turnover      = sell_turnover + buy_turnover

    brokerage   = COSTS["brokerage_per_order"] * n_orders
    exch_fee    = turnover * COSTS["exchange_txn_pct"] / 100
    sebi_fee    = turnover * COSTS["sebi_pct"]        / 100
    stt         = sell_turnover * COSTS["stt_sell_premium_pct"] / 100
    stamp       = buy_turnover  * COSTS["stamp_duty_pct"]       / 100
    gst         = (brokerage + exch_fee) * COSTS["gst_pct"]    / 100

    costs = round(brokerage + exch_fee + sebi_fee + stt + stamp + gst, 2)
    trade["costs_total"] = costs
    trade["pnl_net"]     = round((trade.get("pnl_total") or 0) - costs, 2)


def compute_avg_attribution(trades: list) -> dict:
    """Average Greek attribution across closed trades that have attribution data."""
    cols = ["theta_pnl", "delta_pnl", "vega_pnl", "residual_pnl"]
    result = {}
    for col in cols:
        vals = [t[col] for t in trades if t.get(col) is not None]
        result[col] = round(sum(vals) / len(vals), 0) if vals else None
    if all(result[c] is not None for c in cols):
        gross = sum(abs(result[c] or 0) for c in cols) or 1.0
        t_share = (result["theta_pnl"] or 0) / gross
        d_share = abs(result["delta_pnl"] or 0) / gross
        result["verdict"] = (
            "THETA-DRIVEN" if t_share > 0.5 else
            "DIRECTION-DRIVEN" if d_share > 0.4 else "MIXED"
        )
    return result


def compute_session_pnl():
    """Compute today's realized + unrealized P&L across all trades. Returns (gross, net)."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    gross_r = net_r = gross_u = net_u = 0.0
    for t in state.paper_trades:
        if t.get("status") != "OPEN" and (t.get("exit_time") or "").startswith(today):
            compute_trade_costs(t)
            gross_r += t.get("pnl_total", 0) or 0
            net_r   += t.get("pnl_net",   0) or 0
        elif t.get("status") == "OPEN":
            gross_u += t.get("pnl_total", 0) or 0
            net_u   += t.get("pnl_total", 0) or 0   # unrealized: no costs yet
    return round(gross_r + gross_u, 2), round(net_r + net_u, 2)


def target_decay_for_dte(dte):
    try:
        from config import TARGET_DECAY_BY_DTE, TARGET_DECAY_DEFAULT
        return TARGET_DECAY_BY_DTE.get(min(int(dte or 5), 7), TARGET_DECAY_DEFAULT)
    except Exception:
        return 0.50


def _dte_adjusted_stop_mult(dte: float) -> float:
    """DTE-conditional stop multiplier. Tighter near expiry where gamma is explosive."""
    try:
        from oi_dashboard.trading.paper_engine import dte_adjusted_stop_mult
        return dte_adjusted_stop_mult(dte)
    except Exception:
        if dte < 2.0:   return 1.25
        elif dte < 4.0: return 1.50
        elif dte < 6.0: return 1.75
        else:           return 2.00


def update_trailing(trade, net_cur, pnl_unit, lots, time_str):
    """
    Trailing stop from peak.
    Activates once (entry - net_cur) / entry >= TRAILING_ACTIVATION_DECAY.
    Tracks lowest net_cur seen (best fill). Exits if net_cur rises by
    TRAILING_GIVEBACK_PCT * entry above the best.
    Returns True if trailing stop fired.
    """
    try:
        from config import TRAILING_ACTIVATION_DECAY, TRAILING_GIVEBACK_PCT
    except Exception:
        TRAILING_ACTIVATION_DECAY = 0.25
        TRAILING_GIVEBACK_PCT     = 0.30

    entry      = trade["net_credit_entry"]
    decay_pct  = (entry - net_cur) / entry if entry else 0

    if not trade.get("trail_active"):
        if decay_pct >= TRAILING_ACTIVATION_DECAY:
            trade["trail_active"]   = True
            trade["trail_best_net"] = net_cur
            trade["trail_stop_net"] = round(net_cur + entry * TRAILING_GIVEBACK_PCT, 2)
            print(f"[TRAIL] #{trade['id']} activated: best=₹{net_cur:.2f} "
                  f"stop=₹{trade['trail_stop_net']:.2f}")
        return False

    if net_cur < (trade.get("trail_best_net") or net_cur):
        trade["trail_best_net"] = net_cur
        trade["trail_stop_net"] = round(net_cur + entry * TRAILING_GIVEBACK_PCT, 2)

    if net_cur >= trade["trail_stop_net"]:
        trade.update(
            status      = "TRAIL_STOP",
            exit_time   = time_str,
            exit_reason = "TRAIL_STOP",
            exit_net    = round(net_cur, 2),
            pnl_per_lot = round(pnl_unit * LOT_SIZE, 2),
            pnl_total   = round(pnl_unit * LOT_SIZE * lots, 2),
        )
        print(f"[TRAIL] #{trade['id']} STOPPED net=₹{net_cur:.2f} "
              f"stop=₹{trade['trail_stop_net']:.2f} pnl=₹{trade['pnl_total']:.0f}")
        return True

    return False


def check_gap_reversal_exit(spot, time_str):
    """Exit open directional spread trades if the gap has fully reversed past prev_close."""
    changed = False
    _GAP_TYPES = {"GAP_SPREAD", "PUT_SPREAD", "CALL_SPREAD"}
    for t in list(state.paper_trades):
        if t.get("trade_type") not in _GAP_TYPES or t.get("status") != "OPEN":
            continue
        prev_close = t.get("prev_close", 0) or 0
        open_price = t.get("open_price", 0) or 0
        direction  = t.get("direction", "")
        if not prev_close or not open_price or not direction:
            continue
        gap_size = abs(open_price - prev_close)
        if direction == "GAP_DOWN" and spot > prev_close:
            close_trade_now(t, time_str, "GAP_REVERSAL_EXIT",
                             f"gap filled: spot {spot:.2f} > prev_close {prev_close:.2f}")
            changed = True
            continue
        if direction == "GAP_UP" and spot < prev_close:
            close_trade_now(t, time_str, "GAP_REVERSAL_EXIT",
                             f"gap filled: spot {spot:.2f} < prev_close {prev_close:.2f}")
            changed = True
            continue
        # 75% fill warning (no exit)
        if gap_size > 0:
            fill = (spot - open_price) / gap_size if direction == "GAP_DOWN" else (open_price - spot) / gap_size
            if fill >= 0.75:
                t["reversal_warning"] = f"gap {fill*100:.0f}% filled"
            else:
                t.pop("reversal_warning", None)
    if changed:
        save_paper_trades()


def check_carry_prompt(now_ist_obj):
    if state.carry_prompt_pending:
        return
    if now_ist_obj.hour == 15 and now_ist_obj.minute >= 25:
        open_trades = [t for t in state.paper_trades if t.get("status") == "OPEN"]
        if open_trades:
            state.carry_prompt_pending = True
            print(f"[CARRY] Prompt triggered at {now_ist_obj.strftime('%H:%M')} — {len(open_trades)} open trade(s)")


def safe_evaluate_carry_forward(t, spot, gap_stats):
    """
    SAFETY WRAPPER: Protects entire dashboard from crashes in evaluate_carry_forward().
    
    This wrapper was added to fix Saturday's VIX-adaptive hedge changes that introduced
    a bug in deep_otm_signal.evaluate_carry_forward(). It prevents that function's 
    exceptions from crashing the entire data pipeline.
    
    Returns safe fallback dict if evaluation fails, preventing dashboard data loss.
    """
    try:
        from oi_dashboard.analytics.deep_otm_signal import evaluate_carry_forward
        result = evaluate_carry_forward(t, spot, gap_stats)
        if result is None:
            return {}
        return result
    except KeyError as e:
        print(f"[CARRY_FWD] KeyError: {e} — likely missing config constant or field")
        return {}
    except TypeError as e:
        print(f"[CARRY_FWD] TypeError: {e} — likely VIX/type issue or None value")
        return {}
    except Exception as e:
        print(f"[CARRY_FWD] {type(e).__name__}: {e}")
        return {}

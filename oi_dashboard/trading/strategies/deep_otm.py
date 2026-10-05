"""Deep-OTM hedged spread capturing inflated implied volatility."""

from datetime import datetime

from oi_dashboard.analytics.deep_otm_signal import check_deep_otm_exit, deep_otm_size
from oi_dashboard.analytics.pricing import get_atm_iv
from oi_dashboard.analytics.realized_vol import get_all_rvs
from oi_dashboard.core import state
from oi_dashboard.core.constants import DEFAULT_LOTS, IST, LOT_SIZE
from oi_dashboard.storage.session_store import save_paper_trades
from oi_dashboard.trading.activity_log import log_auto
from oi_dashboard.trading.fills import fetch_fresh_exit_prices, fetch_leg_or_chain, fill_confirm
from oi_dashboard.trading.rules import can_enter


def enter_deep_otm_spread(chain_out, spot, dte, time_str, side, signal, expiry_dt=""):
    """Enter a hedged Deep OTM spread (sell inflated IV, buy protection 300pt further)."""

    try:
        from config import DEEP_OTM_HEDGE_OFFSET
    except Exception:
        DEEP_OTM_HEDGE_OFFSET = 300

    sell_s = signal["strike"]
    offset = DEEP_OTM_HEDGE_OFFSET
    buy_s  = round((sell_s - offset) / 50) * 50 if side == "PE" else round((sell_s + offset) / 50) * 50
    side_l = side.lower()

    key = (sell_s, buy_s, "DEEP_OTM_SPREAD")
    if key in state.paper_trade_ids:
        print(f"[DEEP OTM] duplicate key {key}, skip")
        return

    ok, reason = can_enter("DEEP_OTM_SPREAD")
    if not ok:
        print(f"[DEEP OTM] blocked: {reason}")
        return

    cmap       = {r["strike"]: r for r in chain_out}
    sell_price = fetch_leg_or_chain(cmap, sell_s, side_l, "sell", expiry_dt)
    buy_price  = fetch_leg_or_chain(cmap, buy_s,  side_l, "buy",  expiry_dt)

    if sell_price is None or buy_price is None or sell_price <= 0:
        print(f"[DEEP OTM REJECT] untradeable: SELL {sell_s}={sell_price} BUY {buy_s}={buy_price}")
        return

    net = round(sell_price - buy_price, 2)
    if net < 1.0:
        print(f"[DEEP OTM REJECT] net credit ₹{net:.2f} too small")
        return

    confirmed, avg = fill_confirm(key, {"sell": sell_price, "buy": buy_price})
    if not confirmed:
        return
    sell_price = avg.get("sell", sell_price)
    buy_price  = avg.get("buy",  buy_price)
    net        = round(sell_price - buy_price, 2)

    lots        = deep_otm_size(signal)
    max_loss    = round((abs(sell_s - buy_s) - net) * LOT_SIZE * lots, 2)
    atm_iv_now  = get_atm_iv(chain_out, spot)

    trade = {
        "id":                   len(state.paper_trades) + 1,
        "trade_type":           "DEEP_OTM_SPREAD",
        "side":                 side,
        "entry_time":           time_str,
        "entry_spot":           round(spot, 2),
        "lots":                 lots,
        "dte_at_entry":         dte,
        "sell_strike":          sell_s,
        "sell_entry":           sell_price,
        "sell_current":         sell_price,
        "buy_strike":           buy_s,
        "buy_entry":            buy_price,
        "buy_current":          buy_price,
        "net_credit_entry":     net,
        "net_credit_current":   net,
        "target_net":           round(net * 0.30, 2),   # 70% decay target
        "stop_net":             round(net * 3.0,  2),
        "max_loss_total":       max_loss,
        "status":               "OPEN",
        "exit_time":            None,
        "exit_reason":          None,
        "pnl_per_lot":          0.0,
        "pnl_total":            0.0,
        "max_adverse_spot":     999.0,
        "carry_forward":        False,
        "entry_reason":         f"IV inflation {signal['inflation_abs']:.1f}pp on {sell_s} {side}",
        "lot_size_used":        LOT_SIZE,
        "entry_atm_iv":         atm_iv_now,
        "entry_iv_inflation":   signal.get("inflation_abs", 0),
        "entry_rv20":           round((get_all_rvs().get("rv20") or 0) * 100, 2),
        "iv_capture_pct":       None,
        "target_decay_pct":     0.70,
        "target_credit":        round(net * 0.30, 2),
        "trail_active":         False,
        "trail_best_net":       None,
        "trail_stop_net":       None,
        "mae_credit":           net,
        "mae_time":             None,
        "mae_pnl":              0.0,
        "expiry_dt":            expiry_dt,
        "leg_prices":           {},
        "last_leg_update":      None,
    }
    # Mirror side-prefixed fields for Position Risk panel
    if side == "PE":
        trade.update({
            "pe_sell_strike": sell_s, "pe_sell_entry": sell_price, "pe_sell_current": sell_price,
            "pe_buy_strike":  buy_s,  "pe_buy_entry":  buy_price,  "pe_buy_current":  buy_price,
        })
    else:
        trade.update({
            "ce_sell_strike": sell_s, "ce_sell_entry": sell_price, "ce_sell_current": sell_price,
            "ce_buy_strike":  buy_s,  "ce_buy_entry":  buy_price,  "ce_buy_current":  buy_price,
        })

    state.paper_trades.append(trade)
    state.paper_trade_ids.add(key)
    save_paper_trades()
    msg = (f"SELL {sell_s} {side} @ ₹{sell_price:.2f} | BUY {buy_s} {side} @ ₹{buy_price:.2f} | "
           f"Net ₹{net:.2f} | {lots}L | max_loss ₹{max_loss:,.0f} | "
           f"IV inflation {signal['inflation_abs']:.1f}pp | Trade #{trade['id']}")
    print(f"[DEEP OTM ENTRY] {msg}")
    log_auto("DEEP_OTM_ENTRY", msg)


def update_deep_otm_spread(trade, chain_out, spot, time_str):
    """Refresh prices and check exits for an open DEEP_OTM_SPREAD."""
    cmap    = {r["strike"]: r for r in chain_out}
    side    = trade.get("side", "PE")
    ltp_key = "pe_ltp" if side == "PE" else "ce_ltp"
    now_ist = datetime.now(IST)

    sell_cur = float((cmap.get(trade["sell_strike"]) or {}).get(ltp_key)
                     or trade.get("sell_current") or trade.get("sell_entry", 0))
    buy_cur  = float((cmap.get(trade["buy_strike"])  or {}).get(ltp_key)
                     or trade.get("buy_current")  or trade.get("buy_entry",  0))
    trade["sell_current"] = round(sell_cur, 2)
    trade["buy_current"]  = round(buy_cur,  2)

    net_cur = sell_cur - buy_cur
    trade["net_credit_current"] = round(net_cur, 2)

    # MAE
    if net_cur > trade.get("mae_credit", trade["net_credit_entry"]):
        trade["mae_credit"] = round(net_cur, 2)
        trade["mae_time"]   = time_str
        lots_m = trade.get("lots", DEFAULT_LOTS)
        trade["mae_pnl"] = round((trade["net_credit_entry"] - net_cur) * LOT_SIZE * lots_m, 2)

    lots     = trade.get("lots", DEFAULT_LOTS)
    pnl_unit = trade["net_credit_entry"] - net_cur
    trade["pnl_per_lot"] = round(pnl_unit * LOT_SIZE, 2)
    trade["pnl_total"]   = round(pnl_unit * LOT_SIZE * lots, 2)

    exit_args = dict(pnl_per_lot=trade["pnl_per_lot"], pnl_total=trade["pnl_total"])

    # EOD
    if now_ist.hour == 15 and now_ist.minute >= 28 and not trade.get("carry_forward"):
        fetch_fresh_exit_prices(trade)
        trade.update(status="EOD", exit_time=time_str, exit_reason="EOD", **exit_args)
        return

    # Smart exit via deep_otm_signal
    exit_reason = check_deep_otm_exit(trade, spot, cmap, time_str)
    if exit_reason:
        fetch_fresh_exit_prices(trade)
        trade.update(status=exit_reason, exit_time=time_str, exit_reason=exit_reason, **exit_args)
        print(f"[DEEP OTM EXIT] {exit_reason} pnl=₹{trade['pnl_total']:.0f}")

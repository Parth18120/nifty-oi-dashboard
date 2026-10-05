"""Gap-day directional credit spread: entry and management."""

from datetime import datetime

from oi_dashboard.analytics.iv_crush import check_iv_crush_exit
from oi_dashboard.analytics.pricing import get_atm_iv
from oi_dashboard.analytics.realized_vol import get_all_rvs
from oi_dashboard.core import state
from oi_dashboard.core.constants import DEFAULT_LOTS, IST, LOT_SIZE
from oi_dashboard.storage.session_store import save_paper_trades
from oi_dashboard.trading.accounting import (
    _dte_adjusted_stop_mult,
    target_decay_for_dte,
    update_trailing,
)
from oi_dashboard.trading.activity_log import log_auto
from oi_dashboard.trading.fills import fetch_fresh_exit_prices, fetch_leg_or_chain, fill_confirm
from oi_dashboard.trading.rules import can_enter, sweet_zone


def enter_gap_trade(chain_out, spot, dte, time_str, direction,
                     prev_close=0, open_price=0, expiry_dt="",
                     spread_type="GAP_SPREAD", classifier_mode=""):
    """
    Directional spread entry. spread_type controls what gets recorded:
      GAP_SPREAD   — legacy, direction-only (GAP_UP/GAP_DOWN)
      PUT_SPREAD   — GAP_UP / REVERSAL_DOWN: sell PE spread
      CALL_SPREAD  — GAP_DOWN / REVERSAL_UP: sell CE spread
    """

    ok, reason = can_enter(spread_type, direction)
    if not ok:
        msg = f"[{spread_type} ENTRY] blocked: {reason}"
        print(msg)
        log_auto(f"{spread_type}_SKIP", msg)
        return

    cmap = {r["strike"]: r for r in chain_out}
    otm_min, otm_max = sweet_zone(dte)

    try:
        from config import MIN_LTP as _GAP_MIN_LTP
    except ImportError:
        _GAP_MIN_LTP = 15.0
    if direction == "GAP_UP":
        candidates = []
        for row in chain_out:
            s      = row["strike"]
            otm    = spot - s
            pe_ltp = float(row.get("pe_ltp") or 0)
            if otm_min <= otm <= otm_max and pe_ltp >= _GAP_MIN_LTP:
                candidates.append((s, pe_ltp, otm))
        if not candidates:
            print(f"[GAP] No PE candidate in {otm_min}-{otm_max}pt OTM zone (DTE {dte})"); return
        candidates.sort(key=lambda x: x[1], reverse=True)
        sell_s, sell_ltp, otm = candidates[0]
        buy_s      = round((sell_s - 400) / 50) * 50
        sell_price = fetch_leg_or_chain(cmap, sell_s, "pe", "sell", expiry_dt)
        buy_price  = fetch_leg_or_chain(cmap, buy_s,  "pe", "buy",  expiry_dt)
        side = "PE"
    else:
        candidates = []
        for row in chain_out:
            s      = row["strike"]
            otm    = s - spot
            ce_ltp = float(row.get("ce_ltp") or 0)
            if otm_min <= otm <= otm_max and ce_ltp >= _GAP_MIN_LTP:
                candidates.append((s, ce_ltp, otm))
        if not candidates:
            print(f"[GAP] No CE candidate in {otm_min}-{otm_max}pt OTM zone (DTE {dte})"); return
        candidates.sort(key=lambda x: x[1], reverse=True)
        sell_s, sell_ltp, otm = candidates[0]
        buy_s      = round((sell_s + 400) / 50) * 50
        sell_price = fetch_leg_or_chain(cmap, sell_s, "ce", "sell", expiry_dt)
        buy_price  = fetch_leg_or_chain(cmap, buy_s,  "ce", "buy",  expiry_dt)
        side = "CE"

    if sell_price is None or buy_price is None:
        print(f"[GAP REJECT] Untradeable leg: SELL {sell_s} {side}={sell_price} BUY {buy_s} {side}={buy_price}")
        return

    sell_ltp = sell_price
    buy_ltp  = buy_price
    net = round(sell_ltp - buy_ltp, 2)
    print(f"[GAP ENTRY] SELL {sell_s} {side} @ ₹{sell_ltp} (bid) | BUY {buy_s} {side} @ ₹{buy_ltp} (ask) | Net=₹{net:.2f}")
    if net < 3.0:
        print(f"[GAP] Net credit ₹{net:.2f} too low"); return
    if buy_ltp > sell_ltp * 0.60:
        print(f"[GAP WARN] Hedge cost ₹{buy_ltp:.2f} = {buy_ltp/sell_ltp*100:.0f}% of sell. Wide protection flagged.")

    lots = DEFAULT_LOTS
    key  = (sell_s, buy_s, spread_type)
    if key in state.paper_trade_ids:
        return

    confirmed, avg = fill_confirm(key, {"sell": sell_ltp, "buy": buy_ltp})
    if not confirmed:
        return
    sell_ltp = avg.get("sell", sell_ltp)
    buy_ltp  = avg.get("buy",  buy_ltp)
    net      = round(sell_ltp - buy_ltp, 2)

    trade = {
        "id":                 len(state.paper_trades) + 1,
        "trade_type":         spread_type,
        "direction":          direction,
        "side":               side,
        "entry_time":         time_str,
        "entry_spot":         round(spot, 2),
        "lots":               lots,
        "dte_at_entry":       dte,
        "sell_strike":        sell_s,
        "sell_entry":         sell_ltp,
        "sell_current":       sell_ltp,
        "buy_strike":         buy_s,
        "buy_entry":          buy_ltp,
        "buy_current":        buy_ltp,
        "net_credit_entry":   net,
        "net_credit_current": net,
        "target_net":         round(net * (1 - target_decay_for_dte(dte)), 2),
        "stop_net":           round(net * _dte_adjusted_stop_mult(dte), 2),
        "status":             "OPEN",
        "exit_time":          None,
        "exit_reason":        None,
        "pnl_per_lot":        0.0,
        "pnl_total":          0.0,
        "max_adverse_spot":   999.0,
        "carry_forward":      False,
        "entry_reason":       "auto",
        "lot_size_used":      LOT_SIZE,
        "prev_close":         round(prev_close, 2),
        "open_price":         round(open_price, 2),
        "entry_atm_iv":       get_atm_iv(chain_out, spot),
        "entry_rv20":         round((get_all_rvs().get("rv20") or 0) * 100, 2),
        "iv_capture_pct":     None,
        "target_decay_pct":   target_decay_for_dte(dte),
        "target_credit":      round(net * (1 - target_decay_for_dte(dte)), 2),
        "trail_active":       False,
        "trail_best_net":     None,
        "trail_stop_net":     None,
        "leg_subscription":   [(sell_s, side), (buy_s, side)],
        "leg_prices":         {},
        "last_leg_update":    None,
        "expiry_dt":          expiry_dt,
        "classifier_mode":    classifier_mode or direction,
        # MAE (Max Adverse Excursion) — worst net credit seen while open
        "mae_credit":         net,
        "mae_time":           None,
        "mae_pnl":            0.0,
    }
    # Mirror PE/CE-prefixed fields for Position Risk panel compatibility
    if side == "PE":
        trade.update({
            "pe_sell_strike": sell_s, "pe_sell_entry": sell_ltp, "pe_sell_current": sell_ltp,
            "pe_buy_strike":  buy_s,  "pe_buy_entry":  buy_ltp,  "pe_buy_current":  buy_ltp,
            "max_loss_total": round((abs(sell_s - buy_s) - net) * LOT_SIZE * lots, 2),
        })
    else:
        trade.update({
            "ce_sell_strike": sell_s, "ce_sell_entry": sell_ltp, "ce_sell_current": sell_ltp,
            "ce_buy_strike":  buy_s,  "ce_buy_entry":  buy_ltp,  "ce_buy_current":  buy_ltp,
            "max_loss_total": round((abs(sell_s - buy_s) - net) * LOT_SIZE * lots, 2),
        })
    state.paper_trades.append(trade)
    state.paper_trade_ids.add(key)
    save_paper_trades()
    entry_msg = (f"mode={classifier_mode or direction} → {spread_type} | "
                 f"SELL {sell_s} {side} @ {sell_ltp:.2f} · BUY {buy_s} {side} @ {buy_ltp:.2f} | "
                 f"Net credit: {net:.2f} · Trade #{trade['id']} recorded")
    print(f"[{spread_type}] ENTERED {lots}L: {entry_msg}")
    log_auto(f"{spread_type}_ENTRY", entry_msg,
              legs=[{"strike": sell_s, "side": side, "role": "SELL", "price": sell_ltp},
                    {"strike": buy_s,  "side": side, "role": "BUY",  "price": buy_ltp}])


def update_gap_spread(trade, chain_out, spot, time_str):
    """Update gap spread (single-side hedge spread)."""
    cmap    = {r["strike"]: r for r in chain_out}
    side    = trade.get("side", "PE")
    ltp_key = "pe_ltp" if side == "PE" else "ce_ltp"

    sell_cur = float((cmap.get(trade["sell_strike"]) or {}).get(ltp_key)
                     or trade.get("sell_current")
                     or trade.get("pe_sell_current") or trade.get("ce_sell_current")
                     or trade.get("sell_entry", 0))
    buy_cur  = float((cmap.get(trade["buy_strike"])  or {}).get(ltp_key)
                     or trade.get("buy_current")
                     or trade.get("pe_buy_current") or trade.get("ce_buy_current")
                     or trade.get("buy_entry", 0))
    trade["sell_current"] = round(sell_cur, 2)
    trade["buy_current"]  = round(buy_cur,  2)

    net_cur  = sell_cur - buy_cur
    trade["net_credit_current"] = round(net_cur, 2)

    # MAE: credit going UP means trade is WORSE (we are short credit)
    if net_cur > trade.get("mae_credit", trade["net_credit_entry"]):
        trade["mae_credit"] = round(net_cur, 2)
        trade["mae_time"]   = time_str
        lots_m = trade.get("lots", 10)
        trade["mae_pnl"] = round((trade["net_credit_entry"] - net_cur) * LOT_SIZE * lots_m, 2)

    lots     = trade.get("lots", 10)
    pnl_unit = trade["net_credit_entry"] - net_cur
    trade["pnl_per_lot"] = round(pnl_unit * LOT_SIZE, 2)
    trade["pnl_total"]   = round(pnl_unit * LOT_SIZE * lots, 2)

    adv = (spot - trade["sell_strike"]) if side == "PE" else (trade["sell_strike"] - spot)
    prev_adv = trade.get("max_adverse_spot") or 999
    trade["max_adverse_spot"] = round(min(adv, prev_adv), 1)

    now_ist   = datetime.now(IST)
    exit_args = dict(pnl_per_lot=round(pnl_unit*LOT_SIZE,2), pnl_total=round(pnl_unit*LOT_SIZE*lots,2))

    if -net_cur >= trade["stop_net"]:
        fetch_fresh_exit_prices(trade)
        trade.update(status="STOPPED", exit_time=time_str, exit_reason="STOP", **exit_args)
        print(f"[GAP SPREAD] STOPPED pnl=₹{trade['pnl_total']:.0f}")
        return
    if now_ist.hour == 15 and now_ist.minute >= 28 and not trade.get("carry_forward"):
        fetch_fresh_exit_prices(trade)
        trade.update(status="EOD", exit_time=time_str, exit_reason="EOD", **exit_args)
        return
    if update_trailing(trade, net_cur, pnl_unit, lots, time_str):
        fetch_fresh_exit_prices(trade)
        return
    target_credit = trade.get("target_credit") or trade["net_credit_entry"] * 0.60
    if net_cur <= target_credit:
        fetch_fresh_exit_prices(trade)
        trade.update(status="TARGET", exit_time=time_str, exit_reason="TARGET", **exit_args)
        print(f"[GAP SPREAD] TARGET pnl=₹{trade['pnl_total']:.0f}")
        return
    check_iv_crush_exit(trade, chain_out, spot, time_str, pnl_unit, lots)

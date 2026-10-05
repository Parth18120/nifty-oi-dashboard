"""Naked strangle (range markets): entry and management."""

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
from oi_dashboard.trading.fills import fill_confirm
from oi_dashboard.trading.rules import can_enter


def enter_strangle(chain_out, spot, dte, time_str):
    """Naked Strangle — range market, no hedge. Uses OPPORTUNITY_ZONE (200-400pt) for RANGE."""
    from config import OPPORTUNITY_ZONE

    ok, reason = can_enter("STRANGLE")
    if not ok:
        print(f"[STRANGLE ENTRY] blocked: {reason}")
        return

    pe_best = ce_best = None
    pe_sc   = ce_sc   = -1
    opp_min, opp_max  = OPPORTUNITY_ZONE  # 200-400pt for RANGE strangle

    try:
        from config import MIN_LTP as _ST_MIN_LTP
    except ImportError:
        _ST_MIN_LTP = 15.0
    for row in chain_out:
        s      = row["strike"]
        pe_ltp = float(row.get("pe_ltp") or 0)
        otm_pe = spot - s
        if opp_min <= otm_pe <= opp_max and pe_ltp >= _ST_MIN_LTP:
            if pe_ltp > pe_sc:
                pe_sc, pe_best = pe_ltp, (s, pe_ltp, otm_pe)

        ce_ltp = float(row.get("ce_ltp") or 0)
        otm_ce = s - spot
        if opp_min <= otm_ce <= opp_max and ce_ltp >= _ST_MIN_LTP:
            if ce_ltp > ce_sc:
                ce_sc, ce_best = ce_ltp, (s, ce_ltp, otm_ce)

    if not pe_best and not ce_best:
        print(f"[STRANGLE] No qualifying strikes {opp_min}-{opp_max}pt OTM LTP>=₹{_ST_MIN_LTP} (DTE {dte})"); return

    net = 0.0
    if pe_best: net += pe_best[1]
    if ce_best: net += ce_best[1]
    if net < 10.0:
        print(f"[STRANGLE] Net ₹{net:.2f} too low"); return

    lots = DEFAULT_LOTS
    key  = (pe_best[0] if pe_best else 0, ce_best[0] if ce_best else 0, "STRANGLE")
    if key in state.paper_trade_ids:
        return

    st_prices = {}
    if pe_best: st_prices["pe"] = pe_best[1]
    if ce_best: st_prices["ce"] = ce_best[1]
    confirmed, avg = fill_confirm(key, st_prices)
    if not confirmed:
        return
    if pe_best: pe_best = (pe_best[0], avg.get("pe", pe_best[1]), pe_best[2])
    if ce_best: ce_best = (ce_best[0], avg.get("ce", ce_best[1]), ce_best[2])
    net = 0.0
    if pe_best: net += pe_best[1]
    if ce_best: net += ce_best[1]

    st = {
        "id":                 len(state.paper_trades) + 1,
        "trade_type":         "STRANGLE",
        "entry_time":         time_str,
        "entry_spot":         round(spot, 2),
        "lots":               lots,
        "dte_at_entry":       dte,
        "pe_strike":          pe_best[0] if pe_best else 0,
        "pe_entry":           pe_best[1] if pe_best else 0,
        "pe_current":         pe_best[1] if pe_best else 0,
        "pe_stop":            round(pe_best[1] * _dte_adjusted_stop_mult(dte), 2) if pe_best else 0,
        "pe_target":          round(pe_best[1] * (1 - target_decay_for_dte(dte)), 2) if pe_best else 0,
        "pe_status":          "OPEN",
        "ce_strike":          ce_best[0] if ce_best else 0,
        "ce_entry":           ce_best[1] if ce_best else 0,
        "ce_current":         ce_best[1] if ce_best else 0,
        "ce_stop":            round(ce_best[1] * _dte_adjusted_stop_mult(dte), 2) if ce_best else 0,
        "ce_target":          round(ce_best[1] * (1 - target_decay_for_dte(dte)), 2) if ce_best else 0,
        "ce_status":          "OPEN",
        "net_credit_entry":   round(net, 2),
        "net_credit_current": round(net, 2),
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
        "panic_triggered":    False,
        "entry_atm_iv":       get_atm_iv(chain_out, spot),
        "entry_rv20":         round((get_all_rvs().get("rv20") or 0) * 100, 2),
        "iv_capture_pct":     None,
        "target_decay_pct":   target_decay_for_dte(dte),
        "target_credit":      round(net * (1 - target_decay_for_dte(dte)), 2),
        "trail_active":       False,
        "trail_best_net":     None,
        "trail_stop_net":     None,
        "mae_credit":         net,
        "mae_time":           None,
        "mae_pnl":            0.0,
    }
    state.paper_trades.append(st)
    state.paper_trade_ids.add(key)
    save_paper_trades()
    print(f"[STRANGLE] ENTERED {lots}L: "
          f"PE {pe_best[0] if pe_best else '-'} @ ₹{pe_best[1] if pe_best else 0:.2f} "
          f"CE {ce_best[0] if ce_best else '-'} @ ₹{ce_best[1] if ce_best else 0:.2f} "
          f"Net=₹{net:.2f}")


def update_strangle(trade, chain_out, spot, time_str):
    """Update naked strangle — leg stops, net target, EOD."""
    cmap    = {r["strike"]: r for r in chain_out}
    pe_cur  = float((cmap.get(trade["pe_strike"]) or {}).get("pe_ltp") or trade["pe_current"])
    ce_cur  = float((cmap.get(trade["ce_strike"]) or {}).get("ce_ltp") or trade["ce_current"])
    trade["pe_current"] = round(pe_cur, 2)
    trade["ce_current"] = round(ce_cur, 2)

    net_cur  = pe_cur + ce_cur
    trade["net_credit_current"] = round(net_cur, 2)

    if net_cur > trade.get("mae_credit", trade["net_credit_entry"]):
        trade["mae_credit"] = round(net_cur, 2)
        trade["mae_time"]   = time_str
        lots_m = trade.get("lots", 10)
        trade["mae_pnl"] = round((trade["net_credit_entry"] - net_cur) * LOT_SIZE * lots_m, 2)

    lots     = trade.get("lots", 10)
    pnl_unit = trade["net_credit_entry"] - net_cur
    trade["pnl_per_lot"] = round(pnl_unit * LOT_SIZE, 2)
    trade["pnl_total"]   = round(pnl_unit * LOT_SIZE * lots, 2)

    pe_dist = spot - trade["pe_strike"]
    ce_dist = trade["ce_strike"] - spot
    trade["max_adverse_spot"] = round(min(pe_dist, ce_dist), 1)

    now_ist   = datetime.now(IST)
    exit_args = dict(pnl_per_lot=round(pnl_unit*LOT_SIZE,2), pnl_total=round(pnl_unit*LOT_SIZE*lots,2))

    if pe_cur >= trade["pe_stop"] and trade.get("pe_status") == "OPEN":
        trade["pe_status"] = "STOPPED"
        print(f"[STRANGLE] PE stopped @ ₹{pe_cur:.2f}")
    if ce_cur >= trade["ce_stop"] and trade.get("ce_status") == "OPEN":
        trade["ce_status"] = "STOPPED"
        print(f"[STRANGLE] CE stopped @ ₹{ce_cur:.2f}")

    # Exit priority: STOP → EOD → TRAIL → DTE-TARGET → IV_CRUSH
    if trade.get("pe_status") == "STOPPED" and trade.get("ce_status") == "STOPPED":
        trade.update(status="STOPPED", exit_time=time_str, exit_reason="BOTH_LEGS_STOPPED", **exit_args)
        return
    if -net_cur >= trade["stop_net"]:
        trade.update(status="STOPPED", exit_time=time_str, exit_reason="STOP", **exit_args)
        return
    if now_ist.hour == 15 and now_ist.minute >= 28 and not trade.get("carry_forward"):
        trade.update(status="EOD", exit_time=time_str, exit_reason="EOD", **exit_args)
        return
    if update_trailing(trade, net_cur, pnl_unit, lots, time_str):
        return
    target_credit = trade.get("target_credit") or trade["net_credit_entry"] * 0.60
    if net_cur <= target_credit:
        trade.update(status="TARGET", exit_time=time_str, exit_reason="TARGET", **exit_args)
        return
    check_iv_crush_exit(trade, chain_out, spot, time_str, pnl_unit, lots)

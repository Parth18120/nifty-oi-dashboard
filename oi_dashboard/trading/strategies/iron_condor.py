"""Iron-condor strategy: entry and per-refresh management."""

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
from oi_dashboard.trading.fills import (
    fetch_fresh_exit_prices,
    fetch_leg_or_chain,
    fill_confirm,
    get_leg_price,
)
from oi_dashboard.trading.rules import can_enter, sweet_zone


def validate_ic_record(ic):
    """Step 4: sanity-check a fully-built IC dict before saving. Returns (ok, errors)."""
    errors = []
    net = ic.get("net_credit_entry", 0)
    if net <= 0:
        errors.append(f"net_credit_entry {net} <= 0")
    if net > 200:
        errors.append(f"net_credit_entry {net} > 200 (unrealistic)")
    if ic.get("max_loss_total", 0) <= 0:
        errors.append(f"max_loss_total {ic.get('max_loss_total')} <= 0")
    for f in ("pe_sell_strike", "pe_buy_strike", "ce_sell_strike", "ce_buy_strike"):
        if ic.get(f, 0) <= 0:
            errors.append(f"{f}={ic.get(f)} invalid")
    for f in ("pe_sell_entry", "pe_buy_entry", "ce_sell_entry", "ce_buy_entry"):
        if ic.get(f, 0) <= 0:
            errors.append(f"{f}={ic.get(f)} invalid (must be > 0)")
    ce_width = ic.get("ce_buy_strike", 0) - ic.get("ce_sell_strike", 0)
    pe_width = ic.get("pe_sell_strike", 0) - ic.get("pe_buy_strike", 0)
    if ce_width <= 0:
        errors.append(f"CE spread width {ce_width} <= 0 (buy must be above sell)")
    if pe_width <= 0:
        errors.append(f"PE spread width {pe_width} <= 0 (sell must be above buy)")
    return (len(errors) == 0), errors


def enter_iron_condor(chain_out, spot, dte, time_str, expiry_dt=""):
    """Iron Condor — BOTH sides required. Atomic: all 4 legs or nothing."""

    ok, reason = can_enter("IRON_CONDOR")
    if not ok:
        print(f"[IC ENTRY] blocked: {reason}")
        return

    cmap    = {r["strike"]: r for r in chain_out}
    pe_best = ce_best = None
    pe_sc   = ce_sc   = -1
    _ic_otm_min, _ic_otm_max = sweet_zone(dte)

    ce_zone_rows = []

    for row in chain_out:
        s      = row["strike"]
        pe_ltp = float(row.get("pe_ltp") or 0)
        pe_ovr = float(row.get("pe_rv_ovr") or 0)
        otm_pe = spot - s
        try:
            from config import MIN_LTP as _MIN_LTP
        except ImportError:
            _MIN_LTP = 15.0
        if _ic_otm_min <= otm_pe <= _ic_otm_max and pe_ltp >= _MIN_LTP and pe_ovr >= 30:
            sc = pe_ovr * 0.6 + pe_ltp * 0.4
            if sc > pe_sc:
                pe_sc, pe_best = sc, (s, pe_ltp, pe_ovr, otm_pe)

        ce_ltp = float(row.get("ce_ltp") or 0)
        ce_ovr = float(row.get("ce_rv_ovr") or 0)
        otm_ce = s - spot
        if _ic_otm_min <= otm_ce <= _ic_otm_max:
            ce_zone_rows.append((s, ce_ltp, ce_ovr, otm_ce))
            if ce_ltp >= _MIN_LTP and ce_ovr >= 30:
                sc = ce_ovr * 0.6 + ce_ltp * 0.4
                if sc > ce_sc:
                    ce_sc, ce_best = sc, (s, ce_ltp, ce_ovr, otm_ce)

    print(f"[IC START] spot={spot:.1f} dte={dte} zone={_ic_otm_min}-{_ic_otm_max}pt OTM")
    print(f"[IC PE SELL] {'strike=' + str(pe_best[0]) + ' ltp=' + str(pe_best[1]) + ' ovr=' + str(pe_best[2]) if pe_best else 'NONE — no PE in zone with ltp>=8 & ovr>=30'}")
    print(f"[IC CE SELL] {'strike=' + str(ce_best[0]) + ' ltp=' + str(ce_best[1]) + ' ovr=' + str(ce_best[2]) if ce_best else 'NONE — no CE in zone with ltp>=8 & ovr>=30'}")

    # Step 7: dump CE chain slice so we can diagnose missing CE data
    if ce_zone_rows:
        print(f"[IC DEBUG] CE chain slice for {_ic_otm_min}-{_ic_otm_max}pt OTM zone ({len(ce_zone_rows)} rows):")
        for s, ltp, ovr, otm in ce_zone_rows[:10]:
            qual = "✓ QUALIFIES" if ltp >= 8 and ovr >= 30 else f"✗ (ltp={ltp} ovr={ovr:.0f}%)"
            print(f"  {s}: ltp={ltp:.2f} ovr={ovr:.0f}% otm={otm:.0f}pt {qual}")
    else:
        print(f"[IC DEBUG] CE chain has ZERO rows in {_ic_otm_min}-{_ic_otm_max}pt OTM range — chain data gap!")

    if not pe_best:
        msg = f"No PE sell strike in zone (ltp>=8, ovr>=30). IC REJECTED."
        print(f"[IC ABORT] {msg}")
        log_auto("IC_REJECTED", msg)
        return
    if not ce_best:
        msg = f"No CE sell strike in zone (ltp>=8, ovr>=30). IC REJECTED — will NOT record half-IC."
        print(f"[IC ABORT] {msg}")
        log_auto("IC_REJECTED", msg)
        return

    pe_buy_s = round((pe_best[0] - 400) / 50) * 50
    ce_buy_s = round((ce_best[0] + 400) / 50) * 50

    print(f"[IC HEDGE] PE buy strike={pe_buy_s}  CE buy strike={ce_buy_s}")

    pe_sell_price = fetch_leg_or_chain(cmap, pe_best[0], "pe", "sell", expiry_dt)
    pe_buy_price  = fetch_leg_or_chain(cmap, pe_buy_s,  "pe", "buy",  expiry_dt)
    ce_sell_price = fetch_leg_or_chain(cmap, ce_best[0], "ce", "sell", expiry_dt)
    ce_buy_price  = fetch_leg_or_chain(cmap, ce_buy_s,  "ce", "buy",  expiry_dt)

    print(f"[IC PRICES] PE sell={pe_sell_price} PE buy={pe_buy_price}  CE sell={ce_sell_price} CE buy={ce_buy_price}")

    # All 4 prices required — reject on ANY None or zero
    for name, val in [("PE sell", pe_sell_price), ("PE buy", pe_buy_price),
                      ("CE sell", ce_sell_price), ("CE buy", ce_buy_price)]:
        if val is None or val <= 0:
            msg = f"{name} price={val} (invalid). IC REJECTED — no half trades."
            print(f"[IC ABORT] {msg}")
            log_auto("IC_REJECTED", msg)
            return

    pe_buy_ltp = pe_buy_price
    ce_buy_ltp = ce_buy_price
    pe_best    = (pe_best[0], pe_sell_price, pe_best[2], pe_best[3])
    ce_best    = (ce_best[0], ce_sell_price, ce_best[2], ce_best[3])

    net = (pe_best[1] - pe_buy_ltp) + (ce_best[1] - ce_buy_ltp)
    print(f"[IC NET] PE({pe_best[1]:.2f}-{pe_buy_ltp:.2f}) + CE({ce_best[1]:.2f}-{ce_buy_ltp:.2f}) = ₹{net:.2f}")

    hedge_cost  = pe_buy_ltp + ce_buy_ltp
    sell_credit = pe_best[1] + ce_best[1]
    if sell_credit > 0 and hedge_cost > sell_credit * 0.60:
        print(f"[IC WARN] Hedge cost ₹{hedge_cost:.2f} = {hedge_cost/sell_credit*100:.0f}% of sell credit.")
    if net < 5.0:
        msg = f"Net ₹{net:.2f} too low (<5). IC REJECTED."
        print(f"[IC ABORT] {msg}")
        log_auto("IC_REJECTED", msg)
        return

    lots = DEFAULT_LOTS
    key  = (pe_best[0], ce_best[0], "IC")
    if key in state.paper_trade_ids:
        return

    ic_prices = dict(pe_sell=pe_best[1], pe_buy=pe_buy_ltp, ce_sell=ce_best[1], ce_buy=ce_buy_ltp)
    confirmed, avg = fill_confirm(key, ic_prices)
    if not confirmed:
        return

    # Tick-2 price refresh — ALL 4 legs must still be valid
    pe_sell_t2 = get_leg_price(cmap.get(pe_best[0]) or {}, "pe", "sell")
    pe_buy_t2  = get_leg_price(cmap.get(pe_buy_s)   or {}, "pe", "buy")
    ce_sell_t2 = get_leg_price(cmap.get(ce_best[0]) or {}, "ce", "sell")
    ce_buy_t2  = get_leg_price(cmap.get(ce_buy_s)   or {}, "ce", "buy")
    print(f"[IC TICK2] PE sell={pe_sell_t2} buy={pe_buy_t2}  CE sell={ce_sell_t2} buy={ce_buy_t2}")

    for name, val in [("tick2 PE sell", pe_sell_t2), ("tick2 PE buy", pe_buy_t2),
                      ("tick2 CE sell", ce_sell_t2), ("tick2 CE buy", ce_buy_t2)]:
        if val is None or val <= 0:
            msg = f"{name} price={val} (invalid on tick2). IC REJECTED — no half trades."
            print(f"[IC ABORT] {msg}")
            log_auto("IC_REJECTED", msg)
            return

    pe_best    = (pe_best[0], round((pe_best[1] + pe_sell_t2) / 2, 2), pe_best[2], pe_best[3])
    pe_buy_ltp = round((pe_buy_ltp + pe_buy_t2) / 2, 2)
    ce_best    = (ce_best[0], round((ce_best[1] + ce_sell_t2) / 2, 2), ce_best[2], ce_best[3])
    ce_buy_ltp = round((ce_buy_ltp + ce_buy_t2) / 2, 2)
    net = round((pe_best[1] - pe_buy_ltp) + (ce_best[1] - ce_buy_ltp), 2)

    _atm_iv_entry = get_atm_iv(chain_out, spot)
    _rv20_entry   = round((get_all_rvs().get("rv20") or 0) * 100, 2)

    ic = {
        "id":                   len(state.paper_trades) + 1,
        "trade_type":           "IRON_CONDOR",
        "entry_time":           time_str,
        "entry_spot":           round(spot, 2),
        "lots":                 lots,
        "dte_at_entry":         dte,
        "pe_sell_strike":       pe_best[0],
        "pe_sell_entry":        pe_best[1],
        "pe_sell_current":      pe_best[1],
        "pe_buy_strike":        pe_buy_s,
        "pe_buy_entry":         pe_buy_ltp,
        "pe_buy_current":       pe_buy_ltp,
        "ce_sell_strike":       ce_best[0],
        "ce_sell_entry":        ce_best[1],
        "ce_sell_current":      ce_best[1],
        "ce_buy_strike":        ce_buy_s,
        "ce_buy_entry":         ce_buy_ltp,
        "ce_buy_current":       ce_buy_ltp,
        "net_credit_entry":     net,
        "net_credit_current":   net,
        "max_loss_total":       round((400 - net) * LOT_SIZE * lots, 2),
        "target_net":           round(net * (1 - target_decay_for_dte(dte)), 2),
        "stop_net":             round(net * _dte_adjusted_stop_mult(dte), 2),
        "status":               "OPEN",
        "exit_time":            None,
        "exit_net":             None,
        "exit_reason":          None,
        "pnl_per_lot":          0.0,
        "pnl_total":            0.0,
        "max_adverse_spot":     None,
        "carry_forward":        False,
        "entry_reason":         "auto",
        "lot_size_used":        LOT_SIZE,
        "panic_triggered":      False,
        "insurance_cost_lot":   round(pe_buy_ltp + ce_buy_ltp, 2),
        "insurance_cost_total": round((pe_buy_ltp + ce_buy_ltp) * LOT_SIZE * lots, 2),
        "entry_atm_iv":         _atm_iv_entry,
        "entry_rv20":           _rv20_entry,
        "iv_capture_pct":       None,
        "target_decay_pct":     target_decay_for_dte(dte),
        "target_credit":        round(net * (1 - target_decay_for_dte(dte)), 2),
        "trail_active":         False,
        "trail_best_net":       None,
        "trail_stop_net":       None,
        "leg_subscription":     [(pe_best[0], "PE"), (pe_buy_s, "PE"),
                                 (ce_best[0], "CE"), (ce_buy_s, "CE")],
        "leg_prices":           {},
        "last_leg_update":      None,
        "expiry_dt":            expiry_dt,
        "mae_credit":           net,
        "mae_time":             None,
        "mae_pnl":              0.0,
    }

    valid, val_errors = validate_ic_record(ic)
    if not valid:
        msg = f"IC validation failed: {val_errors}"
        print(f"[IC ABORT] {msg}")
        log_auto("IC_REJECTED", msg)
        return

    state.paper_trades.append(ic)
    state.paper_trade_ids.add(key)
    save_paper_trades()
    print(f"[IC ENTERED] #{ic['id']} {lots}L: "
          f"PE {pe_best[0]}/{pe_buy_s} CE {ce_best[0]}/{ce_buy_s} Net=₹{net:.2f}")
    log_auto("IC_FIRED",
        f"IC entered — 4 legs verified · net ₹{net:.2f} · trade #{ic['id']}",
        legs={
            "pe_sell": f"{pe_best[0]} PE @ ₹{pe_best[1]:.2f}",
            "pe_buy":  f"{pe_buy_s} PE @ ₹{pe_buy_ltp:.2f}",
            "ce_sell": f"{ce_best[0]} CE @ ₹{ce_best[1]:.2f}",
            "ce_buy":  f"{ce_buy_s} CE @ ₹{ce_buy_ltp:.2f}",
            "net":     net,
        }
    )


def update_ic_trade(trade, chain_out, spot, time_str):
    """Update current prices and P&L for open Iron Condor."""
    cmap = {r["strike"]: r for r in chain_out}

    def _best_ltp(strike, side_key, fb):
        """
        Priority: leg_prices cache (real bid/ask from tracking thread)
                  → chain snapshot LTP
                  → last known (fb)
        """
        lp = trade.get("leg_prices", {}).get(f"{strike}_{side_key.upper()}")
        if lp and float(lp.get("ltp") or 0) > 0:
            return float(lp["ltp"])
        row = cmap.get(strike)
        if row:
            v = float(row.get(f"{side_key}_ltp") or 0)
            if v > 0:
                return v
        return float(fb)

    pe_sell_cur = _best_ltp(trade["pe_sell_strike"], "pe", trade["pe_sell_current"])
    pe_buy_cur  = _best_ltp(trade["pe_buy_strike"],  "pe", trade["pe_buy_current"])
    ce_sell_cur = _best_ltp(trade["ce_sell_strike"], "ce", trade["ce_sell_current"])
    ce_buy_cur  = _best_ltp(trade["ce_buy_strike"],  "ce", trade["ce_buy_current"])

    trade["pe_sell_current"] = round(pe_sell_cur, 2)
    trade["pe_buy_current"]  = round(pe_buy_cur,  2)
    trade["ce_sell_current"] = round(ce_sell_cur, 2)
    trade["ce_buy_current"]  = round(ce_buy_cur,  2)

    net_cur = (pe_sell_cur - pe_buy_cur) + (ce_sell_cur - ce_buy_cur)
    trade["net_credit_current"] = round(net_cur, 2)

    if net_cur > trade.get("mae_credit", trade["net_credit_entry"]):
        trade["mae_credit"] = round(net_cur, 2)
        trade["mae_time"]   = time_str
        lots_m = trade.get("lots", DEFAULT_LOTS)
        trade["mae_pnl"] = round((trade["net_credit_entry"] - net_cur) * LOT_SIZE * lots_m, 2)

    lots     = trade.get("lots", DEFAULT_LOTS)
    pnl_unit = trade["net_credit_entry"] - net_cur
    pnl_gross = round(pnl_unit * LOT_SIZE * lots, 2)
    trade["pnl_per_lot"] = round(pnl_unit * LOT_SIZE, 2)
    trade["pnl_total"]   = pnl_gross
    # Keep pnl_net live (gross minus costs already paid at entry)
    trade["pnl_net"]     = round(pnl_gross - trade.get("costs_total", 0), 2)

    print(f"[PNL] trade #{trade['id']}: entry={trade['net_credit_entry']} "
          f"current={trade['net_credit_current']} lots={lots} lot_size={LOT_SIZE} "
          f"per_lot={trade['pnl_per_lot']:.0f} gross={pnl_gross:.0f} "
          f"net={trade['pnl_net']:.0f}")

    pe_dist = spot - trade["pe_sell_strike"]
    ce_dist = trade["ce_sell_strike"] - spot
    trade["max_adverse_spot"] = round(min(pe_dist, ce_dist), 1)

    now_ist = datetime.now(IST)

    # Exit priority: STOP → EOD → TRAIL → DTE-TARGET → IV_CRUSH
    exit_args = dict(exit_net=round(net_cur, 2),
                     pnl_per_lot=round(pnl_unit * LOT_SIZE, 2),
                     pnl_total=round(pnl_unit * LOT_SIZE * lots, 2))

    try:
        from oi_dashboard.analytics import spike_detector as _sd
        _sl_paused       = _sd.is_sl_paused()
        _iv_suspended    = _sd.is_iv_crush_suspended()
    except Exception:
        _sl_paused = _iv_suspended = False

    if -net_cur >= trade["stop_net"]:
        if _sl_paused:
            print(f"[SPIKE PROTECT] #{trade['id']} SL condition met but microstructure spike — pausing SL")
            try:
                _sd.record_action(f"SL paused on IC #{trade['id']}")
            except Exception:
                pass
        else:
            fetch_fresh_exit_prices(trade)
            trade.update(status="STOPPED", exit_time=time_str, exit_reason="STOP", **exit_args)
            print(f"[IC] STOPPED pnl=₹{trade['pnl_total']:.0f}")
            return
    if now_ist.hour == 15 and now_ist.minute >= 28 and not trade.get("carry_forward"):
        fetch_fresh_exit_prices(trade)
        trade.update(status="EOD", exit_time=time_str, exit_reason="EOD", **exit_args)
        print(f"[IC] EOD pnl=₹{trade['pnl_total']:.0f}")
        return
    if update_trailing(trade, net_cur, pnl_unit, lots, time_str):
        fetch_fresh_exit_prices(trade)
        return
    target_credit = trade.get("target_credit") or trade["net_credit_entry"] * 0.60
    if net_cur <= target_credit:
        fetch_fresh_exit_prices(trade)
        trade.update(status="TARGET", exit_time=time_str, exit_reason="TARGET", **exit_args)
        print(f"[IC] TARGET (DTE={trade.get('dte_at_entry','?')} decay={trade.get('target_decay_pct',0.40)*100:.0f}%) "
              f"pnl=₹{trade['pnl_total']:.0f}")
        return
    if not _iv_suspended:
        check_iv_crush_exit(trade, chain_out, spot, time_str, pnl_unit, lots)
    else:
        print(f"[SPIKE PROTECT] #{trade['id']} IV_CRUSH check suspended — microstructure spike active")

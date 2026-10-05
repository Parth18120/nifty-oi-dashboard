"""Automatic entry orchestration for the paper-trading engine."""

from datetime import datetime

from oi_dashboard.analytics.events_calendar import get_today_event
from oi_dashboard.analytics.pe_activity_classifier import (
    check_ce_activity_gate,
    check_pe_activity_gate,
)
from oi_dashboard.core import state
from oi_dashboard.core.constants import IST
from oi_dashboard.trading.activity_log import log_auto
from oi_dashboard.trading.classification import check_path_conditions, classify_market
from oi_dashboard.trading.rules import can_enter, check_ic_feasibility, pick_directional_trade
from oi_dashboard.trading.strategies.gap_spread import enter_gap_trade
from oi_dashboard.trading.strategies.iron_condor import enter_iron_condor
from oi_dashboard.trading.strategies.strangle import enter_strangle


def try_auto_enter(spot_data, chain_out, spot, vix, pcr, dte, time_str, mkt_min, expiry_dt=""):
    """
    Runs every refresh. Decision matrix:
      Directional modes (GAP_UP/GAP_DOWN/REVERSAL_UP/REVERSAL_DOWN) → single-side spread
      Non-directional → IC feasibility check → IC/STRANGLE or SKIP

    Time windows:
      Directional spreads: 9:30am – 11:00am (gap conditions most valid at open)
      IRON_CONDOR:         9:30am – 3:00pm
      STRANGLE:            9:30am – 2:00pm
    """

    if mkt_min < 9 * 60 + 30:
        return

    # Event override — check calendar before classifying
    _ev = get_today_event()
    if _ev:
        _ev_action = _ev.get("action", "")
        _ev_label  = _ev.get("label", _ev.get("type", "EVENT"))
        if _ev_action == "SKIP":
            state.last_market_condition = "EVENT_SKIP"
            state.last_mode_reason = f"EVENT: {_ev_label} · no trades today"
            return
        if _ev_action == "IC_ONLY":
            state.last_mode_reason = f"EVENT: {_ev_label} · IC only"
        elif _ev_action == "REDUCED":
            state.last_mode_reason = f"EVENT: {_ev_label} · reduced sizing"

    _pc = float(spot_data.get("prev_close", 0) or 0)
    _op = float(spot_data.get("open", 0) or 0)

    _mode = classify_market(spot_data, chain_out, spot, vix=vix, pcr=pcr)

    # Capture initial 9:30 mode (once per day)
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    if state.mode_at_930_date != today_str:
        state.mode_at_930      = _mode
        state.mode_at_930_date = today_str
        state.reversal_mode    = None   # reset on new day
        if _mode in ("GAP_UP", "GAP_DOWN"):
            print(f"[AUTO] Initial 9:30 mode captured: {_mode} (prev_close={_pc:.1f})")

    # ── Reversal detection (only for directional initial modes, before noon) ──
    try:
        from config import REVERSAL_BREAK_PTS, REVERSAL_ENABLED, REVERSAL_MAX_TIME
    except Exception:
        REVERSAL_ENABLED   = True
        REVERSAL_MAX_TIME  = "12:00"
        REVERSAL_BREAK_PTS = 20

    if (REVERSAL_ENABLED
            and state.reversal_mode is None
            and state.mode_at_930 in ("GAP_UP", "GAP_DOWN")
            and mkt_min < int(REVERSAL_MAX_TIME.split(":")[0]) * 60 + int(REVERSAL_MAX_TIME.split(":")[1])
            and _pc > 0):

        if state.mode_at_930 == "GAP_UP" and spot < _pc - REVERSAL_BREAK_PTS:
            state.reversal_mode = "REVERSAL_DOWN"
            msg = (f"[REVERSAL] GAP_UP -> REVERSAL_DOWN "
                   f"(spot {spot:.1f} broke prev_close {_pc:.1f} - {REVERSAL_BREAK_PTS}pt)")
            print(msg)
            log_auto("REVERSAL_DETECTED", msg)

        elif state.mode_at_930 == "GAP_DOWN" and spot > _pc + REVERSAL_BREAK_PTS:
            state.reversal_mode = "REVERSAL_UP"
            msg = (f"[REVERSAL] GAP_DOWN -> REVERSAL_UP "
                   f"(spot {spot:.1f} broke prev_close {_pc:.1f} + {REVERSAL_BREAK_PTS}pt)")
            print(msg)
            log_auto("REVERSAL_DETECTED", msg)

    # Apply reversal override (once detected, stays for the day)
    if state.reversal_mode:
        _mode = state.reversal_mode

    state.last_market_condition = _mode
    print(f"[AUTO] mode={_mode} spot={spot:.1f} mkt_min={mkt_min}")

    if _ev and _ev.get("action") == "IC_ONLY":
        if _mode in ("IRON_CONDOR", "OVERPRICED") and mkt_min <= 15 * 60:
            ok, why = can_enter("IRON_CONDOR")
            if ok:
                log_auto("IC_ENTRY_ATTEMPT", f"event IC_ONLY mode={_mode}")
                enter_iron_condor(chain_out, spot, dte, time_str, expiry_dt=expiry_dt)
            elif "blocked" not in (state.last_mode_reason or ""):
                state.last_mode_reason += f" · blocked: {why}"
        return

    _path_ok, _path_reason = check_path_conditions()
    if not _path_ok:
        msg = f"mode={_mode} path gate BLOCKED: {_path_reason}"
        print(f"[AUTO] {msg}")
        log_auto("PATH_SKIP", msg)
        if "blocked" not in (state.last_mode_reason or ""):
            state.last_mode_reason = (state.last_mode_reason or _mode) + f" · path: {_path_reason}"
        return

    log_auto("PATH_OK", f"mode={_mode} path check passed")

    # Applied for trades that involve selling puts (PUT_SPREAD, IC) or calls (CALL_SPREAD, IC)
    _pe_gate_ok, _pe_gate_reason = check_pe_activity_gate(state.pe_activity_cache)
    _ce_gate_ok, _ce_gate_reason = check_ce_activity_gate(state.ce_activity_cache)
    _print_pe = f"PE={_pe_gate_reason}"
    _print_ce = f"CE={_ce_gate_reason}"
    log_auto("ACTIVITY_GATE", f"mode={_mode} {_print_pe} | {_print_ce}")

    # DECISION MATRIX uses these gate results inline per trade type
    _DIRECTIONAL = {"GAP_UP", "GAP_DOWN", "REVERSAL_UP", "REVERSAL_DOWN"}

    if _mode in _DIRECTIONAL and mkt_min <= 11 * 60:
        # Directional: single-side spread — no IC feasibility check needed
        spread_type = pick_directional_trade(_mode)
        if spread_type is None:
            return

        # If a directional trade is already open, do not add a second
        open_dir = [t for t in state.paper_trades
                    if t.get("trade_type") in {"GAP_SPREAD", "PUT_SPREAD", "CALL_SPREAD"}
                    and t.get("status") == "OPEN"]
        if open_dir:
            msg = (f"mode={_mode} → would enter {spread_type} but "
                   f"#{open_dir[0]['id']} ({open_dir[0]['trade_type']}) already OPEN. "
                   f"Existing trade stays open — not adding second directional.")
            print(f"[AUTO] {msg}")
            log_auto("DIRECTIONAL_HOLD", msg)
            return

        # PE gate for PUT_SPREAD; CE gate for CALL_SPREAD
        if spread_type == "PUT_SPREAD" and not _pe_gate_ok:
            msg = f"mode={_mode} → {spread_type} blocked by PE activity gate: {_pe_gate_reason}"
            print(f"[AUTO SKIP] {msg}")
            log_auto("ACTIVITY_SKIP", msg)
            if "blocked" not in (state.last_mode_reason or ""):
                state.last_mode_reason = (state.last_mode_reason or _mode) + f" · {_pe_gate_reason}"
            return
        if spread_type == "CALL_SPREAD" and not _ce_gate_ok:
            msg = f"mode={_mode} → {spread_type} blocked by CE activity gate: {_ce_gate_reason}"
            print(f"[AUTO SKIP] {msg}")
            log_auto("ACTIVITY_SKIP", msg)
            if "blocked" not in (state.last_mode_reason or ""):
                state.last_mode_reason = (state.last_mode_reason or _mode) + f" · {_ce_gate_reason}"
            return

        log_auto("DIRECTIONAL_ENTRY", f"mode={_mode} → entering {spread_type}")
        direction = "GAP_UP" if _mode in ("GAP_UP", "REVERSAL_DOWN") else "GAP_DOWN"
        enter_gap_trade(chain_out, spot, dte, time_str, direction, _pc, _op,
                         expiry_dt=expiry_dt, spread_type=spread_type,
                         classifier_mode=_mode)

    elif _mode in ("IRON_CONDOR", "OVERPRICED") and mkt_min <= 15 * 60:
        # Non-directional: require IC feasibility
        try:
            from config import BYPASS_IC_FEASIBILITY
        except Exception:
            BYPASS_IC_FEASIBILITY = False

        if BYPASS_IC_FEASIBILITY:
            ic_ok = True
            print(f"[AUTO] BYPASS_IC_FEASIBILITY=True — skipping feasibility check for mode={_mode}")
            log_auto("IC_BYPASS", f"mode={_mode} IC feasibility check bypassed via config flag")
        else:
            ic_ok = check_ic_feasibility(chain_out, spot, dte)
        if not ic_ok:
            msg = (f"mode={_mode} IC infeasible "
                   f"(CE or PE zone has no positive-OVR strike). "
                   f"Not falling back to single-side without directional signal.")
            print(f"[AUTO SKIP] {msg}")
            log_auto("IC_SKIP_INFEASIBLE", msg)
            if "blocked" not in (state.last_mode_reason or ""):
                state.last_mode_reason = (state.last_mode_reason or _mode) + " · IC infeasible"
            return
        # IC involves both sides — block if either PE or CE is FEAR
        if not _pe_gate_ok:
            msg = f"mode={_mode} IC blocked by PE activity gate: {_pe_gate_reason}"
            print(f"[AUTO SKIP] {msg}")
            log_auto("ACTIVITY_SKIP", msg)
            return
        if not _ce_gate_ok:
            msg = f"mode={_mode} IC blocked by CE activity gate: {_ce_gate_reason}"
            print(f"[AUTO SKIP] {msg}")
            log_auto("ACTIVITY_SKIP", msg)
            return
        ok, why = can_enter("IRON_CONDOR")
        if not ok:
            if "blocked" not in (state.last_mode_reason or ""):
                state.last_mode_reason = (state.last_mode_reason or _mode) + f" · blocked: {why}"
            print(f"[AUTO SKIP] IRON_CONDOR: {why}")
            log_auto("IC_SKIP", f"mode={_mode} No entry — {why}")
        else:
            log_auto("IC_ENTRY_ATTEMPT", f"mode={_mode} IC feasible → entering IC")
            enter_iron_condor(chain_out, spot, dte, time_str, expiry_dt=expiry_dt)

    elif _mode == "RANGE" and mkt_min <= 14 * 60:
        try:
            from config import BYPASS_IC_FEASIBILITY
        except Exception:
            BYPASS_IC_FEASIBILITY = False
        if BYPASS_IC_FEASIBILITY:
            ic_ok = True
            print(f"[AUTO] BYPASS_IC_FEASIBILITY=True — skipping feasibility check for mode=RANGE")
            log_auto("IC_BYPASS", "mode=RANGE IC feasibility check bypassed via config flag")
        else:
            ic_ok = check_ic_feasibility(chain_out, spot, dte)
        if not ic_ok:
            msg = f"mode=RANGE IC infeasible — SKIP"
            print(f"[AUTO SKIP] {msg}")
            log_auto("RANGE_SKIP_INFEASIBLE", msg)
            return
        if not _pe_gate_ok:
            msg = f"mode=RANGE STRANGLE blocked by PE activity gate: {_pe_gate_reason}"
            print(f"[AUTO SKIP] {msg}")
            log_auto("ACTIVITY_SKIP", msg)
            return
        if not _ce_gate_ok:
            msg = f"mode=RANGE STRANGLE blocked by CE activity gate: {_ce_gate_reason}"
            print(f"[AUTO SKIP] {msg}")
            log_auto("ACTIVITY_SKIP", msg)
            return
        ok, why = can_enter("STRANGLE")
        if not ok:
            if "blocked" not in (state.last_mode_reason or ""):
                state.last_mode_reason = (state.last_mode_reason or _mode) + f" · blocked: {why}"
            print(f"[AUTO SKIP] STRANGLE: {why}")
            log_auto("STRANGLE_SKIP", f"mode=RANGE No entry — {why}")
        else:
            log_auto("STRANGLE_ENTRY_ATTEMPT", "mode=RANGE → entering STRANGLE")
            enter_strangle(chain_out, spot, dte, time_str)

    elif _mode in ("SKIP", "NORMAL", "WAIT") and mkt_min <= 15 * 60:
        # No gap, no OVR spike: check if IC is feasible anyway (positive OVR on both sides)
        ic_ok = check_ic_feasibility(chain_out, spot, dte)
        if ic_ok:
            if not _pe_gate_ok or not _ce_gate_ok:
                _blocked = _pe_gate_reason if not _pe_gate_ok else _ce_gate_reason
                log_auto("ACTIVITY_SKIP", f"mode={_mode} IC feasible but activity gate: {_blocked}")
            else:
                ok, why = can_enter("IRON_CONDOR")
                if ok:
                    log_auto("IC_ENTRY_ATTEMPT", f"mode={_mode} but IC feasible → entering IC")
                    enter_iron_condor(chain_out, spot, dte, time_str, expiry_dt=expiry_dt)
                else:
                    log_auto("IC_SKIP", f"mode={_mode} IC feasible but blocked: {why}")
        else:
            log_auto("AUTO_SKIP", f"mode={_mode} IC infeasible → no trade")

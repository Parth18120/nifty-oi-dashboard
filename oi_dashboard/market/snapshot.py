"""Builds the full dashboard payload: fetches the chain, runs analytics, drives the trading engine."""

import math
import time
from datetime import datetime, timedelta

from oi_dashboard.analytics.deep_otm_signal import (
    check_deep_otm_signal,
    record_iv_snapshot,
    top_iv_inflations,
)
from oi_dashboard.analytics.events_calendar import get_today_event, get_upcoming_event
from oi_dashboard.analytics.intraday import (
    build_spike_api,
    build_spike_log_api,
    compute_choppiness,
    compute_rolling_rv,
    compute_rv_sparkline,
    run_micro_move_detection,
    save_minute_bars_eod,
    update_minute_history,
    update_rv_peak,
)
from oi_dashboard.analytics.iv_crush import try_iv_crush_snapshot
from oi_dashboard.analytics.pe_activity_classifier import (
    analyze_spread_decay,
    append_activity_log,
    classify_ce_activity,
    classify_pe_activity,
)
from oi_dashboard.analytics.pricing import (
    _fractional_dte,
    bs_fair_price,
    bs_fair_with_vix,
    overpricing_pct,
)
from oi_dashboard.analytics.realized_vol import get_all_rvs, get_best_sigma, get_rv20, update_rv20
from oi_dashboard.core import state
from oi_dashboard.core.constants import IST, LOT_SIZE, MARGIN_PER_LOT
from oi_dashboard.core.util import safe_sub
from oi_dashboard.market.mock_data import get_mock_data
from oi_dashboard.market.session import get_cache_seconds, is_market_open
from oi_dashboard.storage.session_store import save_open_ltp_snapshot, save_paper_trades
from oi_dashboard.trading.accounting import (
    check_carry_prompt,
    check_gap_reversal_exit,
    compute_avg_attribution,
    compute_session_pnl,
    safe_evaluate_carry_forward,
)
from oi_dashboard.trading.activity_log import log_auto
from oi_dashboard.trading.auto_entry import try_auto_enter
from oi_dashboard.trading.rules import sweet_zone
from oi_dashboard.trading.strategies.deep_otm import update_deep_otm_spread
from oi_dashboard.trading.strategies.gap_spread import update_gap_spread
from oi_dashboard.trading.strategies.iron_condor import update_ic_trade
from oi_dashboard.trading.strategies.panic_buy import detect_panic_and_buy, update_panic_buy
from oi_dashboard.trading.strategies.strangle import update_strangle


def get_fresh_data():
    if state.demo_mode:
        return get_mock_data()
    now = time.time()
    if now - state.cache_time < get_cache_seconds() and state.cache and "error" not in state.cache:
        return state.cache

    try:
        from config import DIVIDEND_YIELD, MY_POSITIONS, RISK_FREE_RATE

        from oi_dashboard.analytics import chain_analysis as A
        from oi_dashboard.analytics import greeks as G
        from oi_dashboard.feeds import dhan_feed as F

        spot_data = F.fetch_nifty_spot()
        spot      = spot_data.get("ltp", 0)

        # Spot fallback — never let spot=0 break the chain fetch
        if spot and spot > 1000:
            state.last_known_spot = spot
        elif state.last_known_spot and state.last_known_spot > 1000:
            spot = state.last_known_spot
            print(f"[Spot] Using cached: {spot}")
        else:
            print(f"[Spot] FAILED spot={spot} — check Dhan token/connection")
            spot = 24400.0  # emergency fallback

        update_rv20(spot)
        rv20 = get_rv20()
        vix  = F.fetch_india_vix()
        if vix and vix > 0:
            state.last_vix = vix
        # Capture open VIX once per day (first reading after 9:15am)
        _now_for_vix = datetime.now(IST)
        _today_str_v = _now_for_vix.strftime("%Y-%m-%d")
        _mkt_min_v   = _now_for_vix.hour * 60 + _now_for_vix.minute
        if state.vix_open_date != _today_str_v and vix and vix > 0 and _mkt_min_v >= 9 * 60 + 15:
            state.vix_at_open   = vix
            state.vix_open_date = _today_str_v
            print(f"[VIX] Open snapshot: {vix:.2f} at {_now_for_vix.strftime('%H:%M')}")
        rv_base = get_best_sigma()
        rv      = rv_base   # overridden below once DTE is known

        # Always use next future expiry — never DTE=0
        expiries  = F.fetch_expiry_dates()
        _today_d  = datetime.now(IST).date()
        dte       = 7
        expiry_dt = ""
        for _exp in expiries:
            try:
                if datetime.strptime(_exp["expiry"], "%Y-%m-%d").date() > _today_d:
                    dte       = max(_exp["days_to_expiry"], 1)
                    expiry_dt = _exp["expiry"]
                    break
            except Exception:
                continue
        if not expiry_dt and expiries:
            dte       = max(expiries[0]["days_to_expiry"], 1)
            expiry_dt = expiries[0]["expiry"]
        state.current_expiry_dt = expiry_dt  # publish for entry functions

        # Dual-chain expiry: analysis uses nearest expiry including today;
        # trading uses the existing logic that always skips today (DTE >= 1).
        # On expiry day these differ — analysis_chain has the real OI action.
        analysis_expiry = F.nearest_expiry()
        expiry_split    = analysis_expiry != expiry_dt

        # DTE-aware sigma: short-dated options need higher vol to price overnight gap risk.
        # Annualised intraday range (Parkinson approx) from today's spot high/low.
        _s_high = float(spot_data.get("high", 0) or 0)
        _s_low  = float(spot_data.get("low",  0) or 0)
        if _s_high > 0 and _s_low > 0 and _s_high > _s_low:
            intraday_rv = abs(math.log(_s_high / _s_low)) * math.sqrt(252 / (4 * math.log(2)))
        else:
            intraday_rv = 0.0
        # No DTE-scaling: _get_best_sigma() already applies VIX floor + 8% min.
        # DTE-scaling was inflating fair values vs what the Volatility panel shows,
        # causing most options to appear "CHEAP" (negative OVR). Use rv_base uniformly.
        rv = rv_base
        all_rvs_now = get_all_rvs()
        rv5_now = (all_rvs_now.get("rv5") or 0.0)
        print(f"[SIGMA] DTE={dte} sigma={rv*100:.2f}% (rv_base={rv_base*100:.2f}% rv5={rv5_now*100:.1f}% intra={intraday_rv*100:.1f}%)")

        # On expiry day, fetch analysis chain FIRST (today's expiry, real OI walls).
        # Trading chain fetch follows — its call to fetch_option_chain_for_expiry
        # overwrites _prev_chain so OI change deltas stay correct for trading chain.
        if expiry_split:
            print(f"[EXPIRY SPLIT] OI analysis={analysis_expiry} | Trading={expiry_dt}")
            _ac_raw = F.fetch_option_chain_for_expiry(spot, analysis_expiry)
            analysis_chain_raw = _ac_raw if _ac_raw else None
        else:
            analysis_chain_raw = None  # set after trading chain fetch below

        print(f"[Chain] Fetching expiry={expiry_dt} DTE={dte} spot={spot}")
        chain = F.fetch_option_chain_for_expiry(spot, expiry_dt) if expiry_dt else F.fetch_option_chain(spot)
        if not chain:
            print(f"[Chain] Empty — retrying with default fetch")
            chain = F.fetch_option_chain(spot)

        if not expiry_split or not analysis_chain_raw:
            analysis_chain_raw = chain   # non-expiry day: same chain for both purposes

        ts = datetime.now(IST).strftime("%H:%M:%S")

        # OI analysis uses analysis_chain_raw (nearest expiry including today).
        # On expiry day this is today's options — where all the real OI walls live.
        # oi_alerts stays on trading chain: it needs correct OI change deltas.
        pcr              = A.compute_pcr(analysis_chain_raw)
        pcr_lbl, pcr_col = A.pcr_label(pcr)
        max_pain         = A.compute_max_pain(analysis_chain_raw)
        support, resist  = A.find_support_resistance(analysis_chain_raw)
        oi_alerts        = A.compute_oi_buildup(chain)
        iv_skew          = A.compute_iv_skew(analysis_chain_raw, spot)

        # Update history
        A.update_oi_history(analysis_chain_raw, ts)
        A.update_pcr_history(pcr, ts)

        # Compute open OI change for each strike
        open_oi   = A.get_open_oi()
        chain_out = []
        for row in chain:
            s      = row["strike"]
            o_ce   = open_oi.get(s, {}).get("ce_oi", row["ce_oi"])
            o_pe   = open_oi.get(s, {}).get("pe_oi", row["pe_oi"])
            ce_chg_from_open = row["ce_oi"] - o_ce
            pe_chg_from_open = row["pe_oi"] - o_pe

            # Compute gamma exposure (CE gamma - PE gamma) * respective OI
            # Use a rough approximation: gamma proportional to 1/(IV * sqrt(DTE))
            # Exact value computed in greeks module
            from oi_dashboard.analytics import greeks as G2
            ce_g = G2.compute_greeks(spot, s, row["ce_iv"] or 14.0, max(dte, 1), "CE",
                                     RISK_FREE_RATE, DIVIDEND_YIELD)
            pe_g = G2.compute_greeks(spot, s, row["pe_iv"] or 14.0, max(dte, 1), "PE",
                                     RISK_FREE_RATE, DIVIDEND_YIELD)
            net_gamma_exp = (ce_g["gamma"] * row["ce_oi"] -
                             pe_g["gamma"] * row["pe_oi"])

            # Theoretical price vs actual for premium hot indicator
            ce_theo  = ce_g["price"]
            pe_theo  = pe_g["price"]
            ce_ovr   = ((row["ce_ltp"] - ce_theo) / ce_theo * 100) if ce_theo > 0 else 0
            pe_ovr   = ((row["pe_ltp"] - pe_theo) / pe_theo * 100) if pe_theo > 0 else 0

            # RV-based fair value — OVR% only for OTM options
            # ITM options trade at near-parity; the OVR% number is meaningless
            # and pollutes the signal. Mark them as None (shown as "ITM" in UI).
            ce_rv_fair, _ = bs_fair_price(spot, s, rv, dte, "CE")
            pe_rv_fair, _ = bs_fair_price(spot, s, rv, dte, "PE")
            ce_is_itm = s < spot      # call ITM when strike < spot
            pe_is_itm = s > spot      # put  ITM when strike > spot
            ce_rv_ovr = None if ce_is_itm else overpricing_pct(row["ce_ltp"], ce_rv_fair)
            pe_rv_ovr = None if pe_is_itm else overpricing_pct(row["pe_ltp"], pe_rv_fair)

            # Realistic fair value: assumes IV mean-reverts to RV + structural premium (3pp)
            # This is the honest capturable edge — market never fully converges to pure RV.
            try:
                from config import STRUCTURAL_IV_PREMIUM_PP as _SIP
            except Exception:
                _SIP = 0.03
            _expected_iv = rv + _SIP
            ce_rv_realistic_fair, _ = bs_fair_price(spot, s, _expected_iv, dte, "CE")
            pe_rv_realistic_fair, _ = bs_fair_price(spot, s, _expected_iv, dte, "PE")

            if s == 24000:
                _T_diag = _fractional_dte(dte) / 365.0
                all_rvs_diag = get_all_rvs()
                print(f'[NIFTY FAIR SIGMA] sigma_source=_get_best_sigma sigma_value={rv:.4f} '
                      f'rv5={all_rvs_diag.get("rv5",0):.4f} rv20={all_rvs_diag.get("rv20",0):.4f} '
                      f'yz20={all_rvs_diag.get("yz20",0):.4f} structural_premium_added={_SIP:.4f}')
                print(f'[NIFTY SIGMA BREAKDOWN] raw_rv={rv:.4f} '
                      f'plus_structural={rv + _SIP:.4f} '
                      f'final_sigma_used={_expected_iv:.4f}')
                print(f'[NIFTY FAIR DIAG CE] strike=24000 ltp={row["ce_ltp"]} '
                      f'fair={ce_rv_realistic_fair:.2f} sigma_used={_expected_iv:.4f} T={_T_diag:.6f}')
                print(f'[NIFTY FAIR DIAG PE] strike=24000 ltp={row["pe_ltp"]} '
                      f'fair={pe_rv_realistic_fair:.2f} sigma_used={_expected_iv:.4f} T={_T_diag:.6f}')
            ce_rv_realistic_ovr = None if ce_is_itm else overpricing_pct(row["ce_ltp"], ce_rv_realistic_fair)
            pe_rv_realistic_ovr = None if pe_is_itm else overpricing_pct(row["pe_ltp"], pe_rv_realistic_fair)

            # VIX-based fair value
            if vix and vix > 0:
                ce_vix_fair, _ = bs_fair_with_vix(spot, s, vix, dte, "CE")
                pe_vix_fair, _ = bs_fair_with_vix(spot, s, vix, dte, "PE")
                ce_vix_ovr = overpricing_pct(row["ce_ltp"], ce_vix_fair) if ce_vix_fair else None
                pe_vix_ovr = overpricing_pct(row["pe_ltp"], pe_vix_fair) if pe_vix_fair else None
            else:
                ce_vix_fair = pe_vix_fair = ce_vix_ovr = pe_vix_ovr = None

            chain_out.append({**row,
                "ce_chg_from_open": ce_chg_from_open,
                "pe_chg_from_open": pe_chg_from_open,
                "net_gamma_exp":    round(net_gamma_exp, 8),
                "ce_theo_price":    round(ce_theo, 2),
                "pe_theo_price":    round(pe_theo, 2),
                "ce_overpriced_pct": round(ce_ovr, 1),
                "pe_overpriced_pct": round(pe_ovr, 1),
                "ce_rv_fair":              round(ce_rv_fair, 2),
                "pe_rv_fair":              round(pe_rv_fair, 2),
                "ce_rv_ovr":               ce_rv_ovr,
                "pe_rv_ovr":               pe_rv_ovr,
                "ce_rv_realistic_fair":    round(ce_rv_realistic_fair, 2),
                "pe_rv_realistic_fair":    round(pe_rv_realistic_fair, 2),
                "ce_rv_realistic_ovr":     ce_rv_realistic_ovr,
                "pe_rv_realistic_ovr":     pe_rv_realistic_ovr,
                "rv20":                    round(rv * 100, 2),
                "ce_vix_fair":      round(ce_vix_fair, 2) if ce_vix_fair else None,
                "pe_vix_fair":      round(pe_vix_fair, 2) if pe_vix_fair else None,
                "ce_vix_ovr":       round(ce_vix_ovr,  1) if ce_vix_ovr  is not None else None,
                "pe_vix_ovr":       round(pe_vix_ovr,  1) if pe_vix_ovr  is not None else None,
            })

        # Record open overpricing on first fetch (or after server restart)
        if not state.open_overpricing:
            for row in chain_out:
                state.open_overpricing[row["strike"]] = {
                    "ce_ovr": row["ce_rv_ovr"],
                    "pe_ovr": row["pe_rv_ovr"],
                }

        # Record open LTP snapshot on first fetch (any time of day so testing works)
        if not state.open_ltp_snapshot and chain_out:
            for row in chain_out:
                state.open_ltp_snapshot[str(row["strike"])] = {
                    "ce_ltp": row.get("ce_ltp", 0),
                    "pe_ltp": row.get("pe_ltp", 0),
                }
            save_open_ltp_snapshot()
            print(f"[SNAP] OPEN SNAPSHOT SAVED: {len(state.open_ltp_snapshot)} strikes at {datetime.now(IST).strftime('%H:%M:%S')}")

        # Add change-since-open to each row (both OVR% delta and LTP% delta)
        for row in chain_out:
            s = row["strike"]
            open_ce = state.open_overpricing.get(s, {}).get("ce_ovr")
            open_pe = state.open_overpricing.get(s, {}).get("pe_ovr")
            row["ce_rv_ovr_chg"] = round(row["ce_rv_ovr"] - open_ce, 1) if (open_ce is not None and row["ce_rv_ovr"] is not None) else None
            row["pe_rv_ovr_chg"] = round(row["pe_rv_ovr"] - open_pe, 1) if (open_pe is not None and row["pe_rv_ovr"] is not None) else None
            snap = state.open_ltp_snapshot.get(str(s)) or state.open_ltp_snapshot.get(s)
            if snap:
                o_ce = snap.get("ce_ltp") or 0
                o_pe = snap.get("pe_ltp") or 0
                c_ce = row.get("ce_ltp") or 0
                c_pe = row.get("pe_ltp") or 0
                row["ce_ltp_chg_pct"] = round(safe_sub(c_ce, o_ce) / o_ce * 100, 1) if o_ce else None
                row["pe_ltp_chg_pct"] = round(safe_sub(c_pe, o_pe) / o_pe * 100, 1) if o_pe else None
            else:
                row["ce_ltp_chg_pct"] = None
                row["pe_ltp_chg_pct"] = None

        try:
            from oi_dashboard.storage import db as _db_chain
            _now_log = datetime.now(IST)
            if (not hasattr(state.log_chain_ts, 'val') or
                    (_now_log - state.log_chain_ts.val).seconds >= 300):
                if is_market_open():
                    _db_chain.log_option_chain_snapshot(chain_out, spot, expiry_dt)
                    state.log_chain_ts.val = _now_log
        except Exception as _e_chain:
            print(f"[ChainLog] Non-fatal: {_e_chain}")

        now_ist   = datetime.now(IST)
        today_str = now_ist.strftime("%Y-%m-%d")
        time_str  = now_ist.strftime("%H:%M:%S")

        if state.paper_trade_date != today_str:
            state.paper_trades    = []
            state.paper_trade_ids = set()
            state.paper_trade_date = today_str
            state.pending_entries.clear()

        mkt_min = now_ist.hour * 60 + now_ist.minute
        paper_market_open = (9 * 60 + 15) <= mkt_min <= (15 * 60 + 29)

        if paper_market_open and not state.paper_trade_paused:
            # OLD NAKED DETECTION DISABLED — using Iron Condor only
            # for row in chain_out:
            #     s = row["strike"]
            #     for otype, ltp_key, ovr_key, fair_key in (
            #         ("CE", "ce_ltp", "ce_rv_ovr", "ce_rv_fair"),
            #         ("PE", "pe_ltp", "pe_rv_ovr", "pe_rv_fair"),
            #     ):
            #         ovr = float(row.get(ovr_key) or 0)
            #         ltp = float(row.get(ltp_key) or 0)
            #         if ovr >= 40 and ltp >= 2.0 and (s, otype) not in _paper_trade_ids:
            #             _paper_trade_ids.add((s, otype))
            #             _paper_trades.append({
            #                 "id":              len(_paper_trades) + 1,
            #                 "strike":          s,
            #                 "option_type":     otype,
            #                 "entry_time":      time_str,
            #                 "entry_ltp":       ltp,
            #                 "entry_fair":      float(row.get(fair_key) or 0),
            #                 "entry_ovr":       ovr,
            #                 "entry_spot":      spot if spot and spot > 0 else 0.0,
            #                 "stop_price":      round(ltp * 2.0, 2),
            #                 "target_price":    round(ltp * 0.40, 2),
            #                 "current_ltp":     ltp,
            #                 "current_ovr":     ovr,
            #                 "current_spot":    spot,
            #                 "max_ltp":         ltp,
            #                 "min_ltp":         ltp,
            #                 "max_adverse_spot": 0.0,
            #                 "status":          "OPEN",
            #                 "exit_time":       None,
            #                 "exit_ltp":        None,
            #                 "exit_reason":     None,
            #                 "pnl_unit":        0.0,
            #                 "pnl_lot":         0.0,
            #                 "updates":         [],
            #             })

            _now_ist = datetime.now(IST)

            # Spot histories
            state.spot_10min_history.append((_now_ist, spot))
            state.spot_60min_history.append((_now_ist, spot))
            state.spot_10min_history[:] = [(t, s) for t, s in state.spot_10min_history
                                      if t >= _now_ist - timedelta(minutes=15)]
            state.spot_60min_history[:] = [(t, s) for t, s in state.spot_60min_history
                                      if t >= _now_ist - timedelta(minutes=65)]

            update_minute_history(_now_ist, spot)
            _rv_now = compute_rolling_rv(15)
            update_rv_peak(_rv_now)
            run_micro_move_detection(_now_ist)

            # EOD minute bar save at 15:35
            if _now_ist.hour == 15 and _now_ist.minute >= 35:
                save_minute_bars_eod()

            # Daily reset for intraday state
            _today_date_str = _now_ist.strftime("%Y-%m-%d")
            if state.micro_moves_today and state.micro_moves_today[0]["time"].strftime("%Y-%m-%d") != _today_date_str:
                state.micro_moves_today   = []
                state.rv_peak_today       = 0.0
                state.rv_peak_time_today  = ""
                state.minute_bars_saved   = False

            try:
                record_iv_snapshot(chain_out, spot)
                _spot_1h_ago    = state.spot_60min_history[0][1] if state.spot_60min_history else spot
                _spot_chg_1h    = ((spot - _spot_1h_ago) / _spot_1h_ago * 100) if _spot_1h_ago else 0
                _ev_within_3d   = bool(get_upcoming_event(days=3))
                state.deep_otm_pe_inflations = top_iv_inflations(chain_out, spot, "PE")
                state.deep_otm_ce_inflations = top_iv_inflations(chain_out, spot, "CE")
                state.deep_otm_pe_signal, state.deep_otm_pe_reason = check_deep_otm_signal(
                    chain_out, spot, dte, "PE",
                    spot_change_pct_1h=_spot_chg_1h,
                    vix_now=vix or 0,
                    event_within_3d=_ev_within_3d,
                    now_dt=_now_ist,
                )
                state.deep_otm_ce_signal, state.deep_otm_ce_reason = check_deep_otm_signal(
                    chain_out, spot, dte, "CE",
                    spot_change_pct_1h=_spot_chg_1h,
                    vix_now=vix or 0,
                    event_within_3d=_ev_within_3d,
                    now_dt=_now_ist,
                )
                if state.deep_otm_pe_signal:
                    print(f"[DEEP OTM PE] signal: {state.deep_otm_pe_signal['strike']} "
                          f"inflation={state.deep_otm_pe_signal['inflation_abs']:.1f}pp "
                          f"LTP={state.deep_otm_pe_signal['ltp']:.2f}")
                if state.deep_otm_ce_signal:
                    print(f"[DEEP OTM CE] signal: {state.deep_otm_ce_signal['strike']} "
                          f"inflation={state.deep_otm_ce_signal['inflation_abs']:.1f}pp "
                          f"LTP={state.deep_otm_ce_signal['ltp']:.2f}")
            except Exception as _de:
                print(f"[DEEP OTM] signal error: {_de}")

            try:
                _prev_close   = float(spot_data.get("prev_close", 0) or 0)
                _vix_open     = state.vix_at_open if state.vix_at_open and state.vix_at_open > 0 else (vix or 0)
                _spot_chg_pct = ((spot - _prev_close) / _prev_close * 100) if _prev_close > 0 else 0
                state.pe_activity_cache = classify_pe_activity(
                    chain_out, spot, vix or 0, _vix_open, _spot_chg_pct)
                state.ce_activity_cache = classify_ce_activity(
                    chain_out, spot, vix or 0, _vix_open, _spot_chg_pct)
                _now_ist_cls = datetime.now(IST)
                state.spread_decay_cache = analyze_spread_decay(
                    chain_out, spot, _spot_chg_pct, now_ist=_now_ist_cls)

                if not state.spread_decay_cache.get("gated", False):
                    _raw_sig = state.spread_decay_cache.get("combined_signal", "NEUTRAL")
                    state.decay_signal_history.append(_raw_sig)
                    if len(state.decay_signal_history) > 3:
                        state.decay_signal_history.pop(0)
                    if (len(state.decay_signal_history) == 3 and
                            len(set(state.decay_signal_history)) == 1):
                        state.decay_signal_stable = _raw_sig
                        state.decay_stable_ts = _now_ist_cls.strftime("%H:%M")
                    _signal_confirmed  = (len(state.decay_signal_history) == 3 and
                                          len(set(state.decay_signal_history)) == 1)
                    _signal_conflicted = (len(state.decay_signal_history) >= 2 and
                                          len(set(state.decay_signal_history)) > 1)
                else:
                    # Gated: reset history so stale signals don't persist
                    state.decay_signal_history = []
                    _signal_confirmed  = False
                    _signal_conflicted = False

                state.spread_decay_cache["stable_signal"]     = state.decay_signal_stable
                state.spread_decay_cache["stable_ts"]         = state.decay_stable_ts
                state.spread_decay_cache["signal_confirmed"]  = _signal_confirmed
                state.spread_decay_cache["signal_conflicted"] = _signal_conflicted
                state.spread_decay_cache["history_length"]    = len(state.decay_signal_history)
                state.spread_decay_cache["history"]           = list(state.decay_signal_history)

                # Write to log at most once every 30 seconds
                if time.time() - state.activity_log_last_time >= 30:
                    append_activity_log(state.pe_activity_cache, state.ce_activity_cache, spot, vix or 0)
                    state.activity_log_last_time = time.time()
            except Exception as _e:
                print(f"[PE_CLASS] classifier error: {_e}")

            # Session lows for panic detection
            for _row in chain_out:
                for _lk in ["pe_ltp", "ce_ltp"]:
                    _v = float(_row.get(_lk) or 0)
                    if _v > 0:
                        _k = f"{_row['strike']}_{_lk}"
                        if _k not in state.strike_session_lows or _v < state.strike_session_lows[_k]:
                            state.strike_session_lows[_k] = _v

            _mkt_min = _now_ist.hour * 60 + _now_ist.minute

            try:
                from oi_dashboard.analytics import spike_detector as _sd

                # Feed VIX history
                _sd.update_vix_history(vix or 0, datetime.now(IST))

                # Fetch minute bars (once per 60s is enough — inside 30s loop it's a no-op if unchanged)
                _mb = F.fetch_nifty_minute_bars(count=30)
                if _mb:
                    _sd.update_minute_bars(_mb)

                # BankNifty rolling cache — maintain 15-min history
                try:
                    _bn_ltp = F.fetch_banknifty_spot()
                    if _bn_ltp and _bn_ltp > 0:
                        _now_bn = datetime.now(IST)
                        state.bn_history.append((_now_bn, _bn_ltp))
                        # prune > 20 min
                        _bn_cutoff = _now_bn - timedelta(minutes=20)
                        state.bn_history = [x for x in state.bn_history if x[0] > _bn_cutoff]
                        state.last_banknifty = _bn_ltp
                        # 15-min-ago reading
                        _target15 = _now_bn - timedelta(minutes=15)
                        _bn15 = next((ltp for ts, ltp in state.bn_history if ts <= _target15), None)
                        state.banknifty_15m_ago = _bn15 or state.last_banknifty
                except Exception as _bne:
                    print(f"[SPIKE] BankNifty fetch: {_bne}")

                # Detect + classify spike
                _spike_result = _sd.detect_and_classify(
                    spot_now       = spot,
                    vix_now        = vix or 0,
                    banknifty_now  = state.last_banknifty,
                    banknifty_prev = state.banknifty_15m_ago,
                    usdinr_now     = state.usdinr_now,
                    usdinr_prev    = state.usdinr_15m_ago,
                    events_today   = [get_today_event()] if get_today_event() else [],
                )
                if _spike_result:
                    print(f"[SPIKE] {_spike_result['direction']} "
                          f"{_spike_result['move_pts']:+.0f}pt "
                          f"({_spike_result['move_pct']:+.2f}%) → "
                          f"{_spike_result['verdict']} ({_spike_result['confidence']}/6)")
                    for _sn, _sv, _snote in _spike_result["signals"]:
                        print(f"  [{_sn}] {_sv} — {_snote}")

                # Update follow prices for mean-reversion tracking
                _sd.update_spike_follow_prices(spot)

            except Exception as _sde:
                print(f"[SPIKE] detector error: {_sde}")

            # Continuous classifier — runs every refresh, entry gates enforced inside can_enter()
            if chain_out and spot and spot > 1000:
                # Deep OTM diagnostic
                for _drow in chain_out:
                    if abs(_drow["strike"] - spot) > 800:
                        print(f"[DEEP OTM] strike={_drow['strike']} "
                              f"ce_ltp={_drow.get('ce_ltp')} ce_bid={_drow.get('ce_bid')} ce_ask={_drow.get('ce_ask')} "
                              f"pe_ltp={_drow.get('pe_ltp')} pe_bid={_drow.get('pe_bid')} pe_ask={_drow.get('pe_ask')} "
                              f"ce_vol={_drow.get('ce_volume')} pe_vol={_drow.get('pe_volume')}")

                try:
                    _auto_enabled = True
                    try:
                        from config import AUTO_TRADE_ENABLED as _ate
                        _auto_enabled = _ate
                    except ImportError:
                        pass
                    if _auto_enabled:
                        # Rule 4: block new entries during any active spike
                        try:
                            _block_entry = _sd.is_entry_blocked()
                        except Exception:
                            _block_entry = False
                        if _block_entry:
                            _asp = _sd.get_active_spike()
                            print(f"[SPIKE BLOCK] new entry blocked — active spike "
                                  f"{_asp['verdict'] if _asp else '?'} until "
                                  f"{_asp['active_until'].strftime('%H:%M') if _asp else '?'}")
                            log_auto("SPIKE_BLOCK",
                                      f"Entry blocked — active spike ({_asp['verdict'] if _asp else '?'})")
                        else:
                            _now_ts = time.time()
                            if _now_ts - state.last_auto_attempt >= 15:
                                state.last_auto_attempt = _now_ts
                                try_auto_enter(spot_data, chain_out, spot, vix or 0, pcr or 1.0, dte, time_str, _mkt_min, expiry_dt=expiry_dt)
                            # else: data refreshed but AUTO decision interval not reached yet
                    else:
                        pass  # auto-trading disabled — manual entry still works
                except Exception as _ee:
                    print(f"[ENTRY] Error: {_ee}")
                    import traceback
                    traceback.print_exc()

            # Daily date reset (resets VIX open snapshot, etc.)
            _today_str = _now_ist.strftime("%Y-%m-%d")
            if state.ic_entry_date != _today_str:
                state.ic_entry_date = _today_str
                print(f"[DAY] New day {_today_str}")

            # Gap reversal exit check (every refresh)
            try:
                check_gap_reversal_exit(spot, time_str)
            except Exception as _gre:
                print(f"[GAP REVERSAL] check error: {_gre}")

            # Panic Buyer — always watching
            try:
                detect_panic_and_buy(chain_out, spot, dte, time_str)
            except Exception as _pbe:
                print(f"[PANIC] error: {_pbe}")

            # Update all open positions
            for _t in list(state.paper_trades):
                if _t.get("status") != "OPEN":
                    continue
                try:
                    _tt = _t.get("trade_type")
                    if   _tt == "IRON_CONDOR": update_ic_trade(_t, chain_out, spot, time_str)
                    elif _tt in ("GAP_SPREAD", "PUT_SPREAD", "CALL_SPREAD"):
                                               update_gap_spread(_t, chain_out, spot, time_str)
                    elif _tt == "STRANGLE":    update_strangle(_t, chain_out, spot, time_str)
                    elif _tt == "PANIC_BUY":   update_panic_buy(_t, chain_out, spot, time_str)
                    elif _tt == "DEEP_OTM_SPREAD": update_deep_otm_spread(_t, chain_out, spot, time_str)
                except Exception as _upe:
                    print(f"[Update] {_t.get('trade_type')} error: {_upe}")

            chain_map = {r["strike"]: r for r in chain_out}
            for trade in state.paper_trades:
                if trade["status"] != "OPEN":
                    continue
                if trade.get("trade_type") in ("IRON_CONDOR", "GAP_SPREAD", "PUT_SPREAD", "CALL_SPREAD", "STRANGLE", "PANIC_BUY"):
                    continue
                s, otype = trade["strike"], trade["option_type"]
                row = chain_map.get(s, {})
                ltp_key = "ce_ltp" if otype == "CE" else "pe_ltp"
                ovr_key = "ce_rv_ovr" if otype == "CE" else "pe_rv_ovr"
                cur_ltp = float(row.get(ltp_key) or trade["current_ltp"])
                cur_ovr = float(row.get(ovr_key) or trade["current_ovr"])

                trade["current_ltp"]  = round(cur_ltp, 2)
                trade["current_ovr"]  = round(cur_ovr, 1)
                trade["current_spot"] = spot
                trade["pnl_unit"]     = round(trade["entry_ltp"] - cur_ltp, 2)
                trade["pnl_lot"]      = round(trade["pnl_unit"] * LOT_SIZE, 2)
                if cur_ltp > trade["max_ltp"]: trade["max_ltp"] = cur_ltp
                if cur_ltp < trade["min_ltp"]: trade["min_ltp"] = cur_ltp
                if trade["entry_spot"] and trade["entry_spot"] > 0 and spot and spot > 0:
                    adverse = (spot - trade["entry_spot"]) if otype == "CE" else (trade["entry_spot"] - spot)
                    adverse = max(0.0, adverse)   # only track adverse moves, ignore favourable
                    if adverse > trade["max_adverse_spot"]:
                        trade["max_adverse_spot"] = round(adverse, 1)

                trade["updates"].append({"time": time_str, "ltp": cur_ltp, "ovr": cur_ovr, "spot": spot})
                if len(trade["updates"]) > 600:
                    trade["updates"] = trade["updates"][-600:]

                if cur_ltp >= trade["stop_price"]:
                    trade.update(status="STOPPED", exit_time=time_str, exit_ltp=cur_ltp,
                                 exit_reason="STOP_LOSS_2x",
                                 pnl_unit=round(trade["entry_ltp"] - cur_ltp, 2),
                                 pnl_lot=round((trade["entry_ltp"] - cur_ltp) * LOT_SIZE, 2))
                elif cur_ltp <= trade["target_price"]:
                    trade.update(status="TARGET_HIT", exit_time=time_str, exit_ltp=cur_ltp,
                                 exit_reason="PROFIT_60PCT",
                                 pnl_unit=round(trade["entry_ltp"] - cur_ltp, 2),
                                 pnl_lot=round((trade["entry_ltp"] - cur_ltp) * LOT_SIZE, 2))
                elif mkt_min >= 15 * 60 + 28:
                    trade.update(status="EOD_EXIT", exit_time=time_str, exit_ltp=cur_ltp,
                                 exit_reason="EOD_15:29",
                                 pnl_unit=round(trade["entry_ltp"] - cur_ltp, 2),
                                 pnl_lot=round((trade["entry_ltp"] - cur_ltp) * LOT_SIZE, 2))

            save_paper_trades()

        # OI-weighted centre price
        total_oi    = sum(r["ce_oi"] + r["pe_oi"] for r in chain_out)
        oi_centre   = 0.0
        if total_oi > 0:
            oi_centre = sum(r["strike"] * (r["ce_oi"] + r["pe_oi"])
                            for r in chain_out) / total_oi

        # Put wall / call wall (single highest strike)
        put_wall  = max(chain_out, key=lambda r: r["pe_oi"])["strike"] if chain_out else 0
        call_wall = max(chain_out, key=lambda r: r["ce_oi"])["strike"] if chain_out else 0

        # Straddle price and ATM IV at ATM strike.
        # On expiry day use analysis_chain_raw (today's expiry has the meaningful ATM IV).
        atm = round(spot / 50) * 50
        _atm_src = analysis_chain_raw if expiry_split else chain_out
        atm_row = next((r for r in _atm_src if r["strike"] == atm), None)
        straddle_price = 0.0
        atm_iv = 0.0
        if atm_row:
            straddle_price = round((atm_row.get("ce_ltp") or 0) + (atm_row.get("pe_ltp") or 0), 2)
            ce_iv = float(atm_row.get("ce_iv", 0) or 0)
            pe_iv = float(atm_row.get("pe_iv", 0) or 0)
            if ce_iv > 0 and pe_iv > 0:
                atm_iv = round((ce_iv + pe_iv) / 2, 2)
            elif ce_iv > 0:
                atm_iv = round(ce_iv, 2)
            elif pe_iv > 0:
                atm_iv = round(pe_iv, 2)
        sigma_pts = round(get_best_sigma() * spot / math.sqrt(252), 1) if spot else 0
        try:
            from config import STRUCTURAL_IV_PREMIUM_PP as _SIP_diag
        except Exception:
            _SIP_diag = 0.03
        print(f'[NIFTY IV PREMIUM PANEL] displayed_atm_iv={atm_iv:.4f}% '
              f'per_strike_rv={rv:.4f} per_strike_expected_iv={rv + _SIP_diag:.4f} '
              f'delta={(atm_iv/100.0 - (rv + _SIP_diag))*100:.2f}pp')

        # IV Crush daily snapshot (9:20am open, 3:20pm close)
        try_iv_crush_snapshot(atm_iv, spot, datetime.now(IST))

        # Carry prompt at 15:25 — set flag if OPEN trades exist
        check_carry_prompt(datetime.now(IST))

        # Total CE and PE OI
        total_ce_oi = sum(r["ce_oi"] for r in chain_out)
        total_pe_oi = sum(r["pe_oi"] for r in chain_out)

        pos_greeks = None
        if MY_POSITIONS:
            pos_greeks = G.compute_position_greeks(
                MY_POSITIONS, spot, chain_out, dte)

        # Book greeks from live open paper trades
        open_trades = [t for t in state.paper_trades if t.get("status") == "OPEN"]
        pg = G.portfolio_greeks(open_trades, spot, chain_out, max(dte, 0.5))
        book_greeks = {
            "net_delta_rs":  pg["net_delta_rs_per_pt"] if pg else 0.0,
            "net_theta_rs":  pg["net_theta_rs_day"]    if pg else 0.0,
            "net_vega_rs":   pg["net_vega_rs_1pct"]    if pg else 0.0,
            "positions":     len(open_trades),
        }

        all_rvs = get_all_rvs()

        _surface = {}
        try:
            from oi_dashboard.analytics import surface_engine as SE
            _surface = SE.surface_metrics(chain_out, spot, max(dte, 0.1), rv)
            SE.log_surface_snapshot(_surface, spot)
            # Log ATM IV snapshot for VRP calibration history
            if _surface.get('atm_iv') and _surface['atm_iv'] > 0:
                from oi_dashboard.storage import db as _db_snap
                _now_snap = datetime.now(IST)
                _db_snap.log_iv_snapshot(
                    _now_snap.strftime("%Y-%m-%d"),
                    _now_snap.strftime("%H:%M"),
                    _surface['atm_iv'],
                    spot,
                    state.last_vix or 0.0,
                )
            # Log skew history once per session at ~14:00 IST
            _now_ist = datetime.now(IST)
            _today_str = _now_ist.strftime("%Y-%m-%d")
            if (state.skew_logged_today != _today_str and
                    _now_ist.hour == 14 and _now_ist.minute < 5):
                SE.log_skew_history(_surface, spot, _today_str)
                state.skew_logged_today = _today_str
        except Exception as _se_err:
            print(f"[SURFACE] Non-fatal error: {_se_err}")

        _preferred_side = None
        try:
            from oi_dashboard.trading.paper_engine import _preferred_sell_side as _pss
            _sigma_pct = round(get_best_sigma() * 100, 2)
            _preferred_side = _pss(_surface, _sigma_pct)
        except Exception:
            pass

        _vrp_cal = {}
        try:
            from oi_dashboard.analytics.vol_engine import vrp_calibration as _vrp_calibration
            _vrp_cal = _vrp_calibration(days=90)
        except Exception as _vrp_err:
            _vrp_cal = {'insufficient_data': True, 'n_days': 0, 'vrp_ratio_mean': 1.185,
                        'error': str(_vrp_err)}

        _regime_score = {}
        try:
            from oi_dashboard.trading import risk_engine as RE
            _regime_score = RE.regime_score(
                vix=state.last_vix,
                spot_rows=state.ohlc_history,
                atm_iv=_surface.get('atm_iv'),
                sigma_blend=rv,
                skew_rr25=_surface.get('skew_rr25'),
            )
        except Exception as _rs_err:
            print(f"[REGIME] Non-fatal error: {_rs_err}")

        _edge_analytics = {}
        try:
            from oi_dashboard.analytics import analytics_engine as AE
            from oi_dashboard.storage import db as _db_ae
            _all_closed = _db_ae.get_paper_trades_closed(days=180)
            _rs20       = AE.rolling_edge_stats(_all_closed, window=20)
            _rs50       = AE.rolling_edge_stats(_all_closed, window=50)
            _reg_perf   = AE.regime_conditional_perf(_all_closed)
            _eq_curve   = AE.equity_curve(_all_closed[-50:])
            _edge_analytics = {
                "rolling_stats":    _rs20,
                "rolling_stats_50": _rs50,
                "attribution_avg":  compute_avg_attribution(_all_closed),
                "regime_perf":      _reg_perf,
                "equity_curve":     _eq_curve,
            }
            # Run attribution on today's closed trades (non-fatal)
            try:
                _today_closed = [t for t in state.paper_trades if t.get("status") == "CLOSED"]
                if _today_closed:
                    AE.attribute_all_closed(datetime.now(IST).strftime("%Y-%m-%d"))
            except Exception:
                pass
        except Exception as _ae_err:
            print(f"[ANALYTICS] Non-fatal error: {_ae_err}")

        state.cache = {
            "timestamp":      ts,
            "market_open":    is_market_open(),
            "spot":           spot_data,
            "vix":            vix,
            "chain":          chain_out,
            "dte":            dte,
            "sweet_zone":     list(sweet_zone(dte)),
            "expiry":         expiry_dt,
            "expiry_near":    analysis_expiry,
            "expiry_split":   expiry_split,
            "chain_near":     analysis_chain_raw if expiry_split else None,
            "pcr":            pcr,
            "pcr_label":      pcr_lbl,
            "pcr_color":      pcr_col,
            "max_pain":       max_pain,
            "support":        support,
            "resistance":     resist,
            "oi_alerts":      oi_alerts,
            "iv_skew":        iv_skew,
            "skew_reversal_buy":   get_skew_reversal_buy(iv_skew, chain_out, spot),
            "range_breakout_flag": get_range_breakout_flag(
                compute_choppiness(30)[0], build_spike_api()),
            "pos_greeks":     pos_greeks,
            "book_greeks":    book_greeks,
            "oi_history":     A.get_oi_history(),
            "pcr_history":    A.get_pcr_history(),
            "my_positions":   MY_POSITIONS,
            "oi_centre":      round(oi_centre, 2),
            "put_wall":       put_wall,
            "call_wall":      call_wall,
            "straddle_price": straddle_price,
            "total_ce_oi":    total_ce_oi,
            "total_pe_oi":    total_pe_oi,
            "rv20":             round((all_rvs.get("rv20") or 0.0) * 100, 2),
            "rv5":              round((all_rvs.get("rv5")  or 0.0) * 100, 2),
            "rv10":             round((all_rvs.get("rv10") or 0.0) * 100, 2),
            "rv60":             round((all_rvs.get("rv60") or 0.0) * 100, 2),
            "yz20":             round((all_rvs.get("yz20") or 0.0) * 100, 2),
            "rv20_estimated":   (all_rvs.get("rv20") or 0.0) <= 0.01,
            "sigma_used":       round(rv * 100, 2),
            "intraday_rv":      round(intraday_rv * 100, 2) if intraday_rv else 0,
            "atm_iv":           atm_iv,
            "sigma_pts":        sigma_pts,
            "paper_trades":     state.paper_trades,
            "paper_trade_date": state.paper_trade_date,
            "refresh_interval":   get_cache_seconds(),
            "data_age_sec":       round(now - state.last_successful_fetch, 1) if state.last_successful_fetch else None,
            "market_condition":   state.last_market_condition,
            "mode_reason":        state.last_mode_reason,
            "gap_confidence":     state.last_gap_confidence,
            "session_pnl":        (_spnl := compute_session_pnl())[0],
            "session_pnl_net":    _spnl[1],
            "lot_size":           LOT_SIZE,
            "margin_per_lot":     MARGIN_PER_LOT,
            "event_today":        get_today_event(),
            "event_upcoming":     get_upcoming_event(days=3),
            "gap_stats":          state.gap_stats,
            "iv_crush_log":       state.iv_crush_log,
            "auto_activity_log":  state.auto_activity_log[:20],
            "auto_enabled":       bool(__import__('config').AUTO_TRADE_ENABLED),
            "active_spike":       build_spike_api(),
            "spike_log":          build_spike_log_api(),
            "rolling_rv_now":       compute_rolling_rv(15),
            "rolling_rv_peak":      state.rv_peak_today,
            "rolling_rv_peak_time": state.rv_peak_time_today,
            "rv_sparkline":         compute_rv_sparkline(),
            "micro_moves":          [
                {**m, "time": m["time"].strftime("%H:%M")}
                for m in state.micro_moves_today[-10:]
            ],
            "choppiness":       compute_choppiness(30)[0],
            "choppiness_label": compute_choppiness(30)[1],
            "pe_activity":  state.pe_activity_cache,
            "ce_activity":  state.ce_activity_cache,
            "spread_decay": state.spread_decay_cache,
            "deep_otm_pe_signal":     state.deep_otm_pe_signal,
            "deep_otm_pe_reason":     state.deep_otm_pe_reason,
            "deep_otm_ce_signal":     state.deep_otm_ce_signal,
            "deep_otm_ce_reason":     state.deep_otm_ce_reason,
            "deep_otm_pe_inflations": state.deep_otm_pe_inflations[:5],
            "deep_otm_ce_inflations": state.deep_otm_ce_inflations[:5],
            # Carry-forward eval for open DEEP_OTM_SPREAD trades (computed at 15:15)
            "deep_otm_carry": [
                {**safe_evaluate_carry_forward(t, spot, state.gap_stats), "id": t["id"],
                 "sell_strike": t.get("sell_strike"), "side": t.get("side")}
                for t in state.paper_trades
                if t.get("trade_type") == "DEEP_OTM_SPREAD" and t.get("status") == "OPEN"
            ],
            "sensex":       state.sensex_state['data'],
            "sensex_error": state.sensex_state['error'],
            "surface":      _surface,
            "edge_analytics": _edge_analytics,
            "regime_score":  _regime_score,
            "preferred_sell_side": _preferred_side,
            "vrp_calibration": _vrp_cal,
        }
        state.cache_time = now
        state.last_successful_fetch = now

    except Exception as e:
        import traceback as _tb
        _tb.print_exc()
        print(f"[SNAPSHOT ERROR] {type(e).__name__}: {e}")
        state.cache = {
            "error":     str(e),
            "traceback": _tb.format_exc(),
            "timestamp": datetime.now(IST).strftime("%H:%M:%S"),
            "market_open": is_market_open(),
            "stale": True,
        }

    return state.cache


def get_skew_reversal_buy(iv_skew, chain, spot):
    """
    Surfaces the near-ATM strike on the IV-cheap side as a skew-reversal buy.
    Reads the existing iv_skew dict (pe_iv - ce_iv convention):
      skew > 0  → PEs expensive, CEs cheap → buy CE
      skew < 0  → CEs expensive, PEs cheap → buy PE
    Returns None when skew is near-neutral (|skew| < 1pp) or no liquid candidate.
    """
    if not iv_skew or not chain:
        return None
    skew = iv_skew.get("skew", 0.0)
    if abs(skew) < 1.0:    # near-neutral skew — no edge
        return None
    cheap_side = "CE" if skew > 0 else "PE"
    ltp_key    = f"{cheap_side.lower()}_ltp"
    iv_key     = f"{cheap_side.lower()}_iv"
    # Near-ATM window: within 250pt of spot (Nifty 50pt step × 5 ≈ 250)
    candidates = [
        r for r in chain
        if abs(r["strike"] - spot) <= 250
        and r.get(ltp_key, 0) >= 2
        and r.get(iv_key, 0) > 0
    ]
    if not candidates:
        return None
    best = min(candidates, key=lambda r: abs(r["strike"] - spot))
    return {
        "type":     "SKEW_REVERSAL",
        "side":     cheap_side,
        "strike":   best["strike"],
        "ltp":      round(best.get(ltp_key, 0), 2),
        "iv":       round(best.get(iv_key, 0), 1),
        "skew":     skew,
        "skew_label": iv_skew.get("label", ""),
    }


def get_range_breakout_flag(choppiness, active_spike):
    """
    Checks whether today's expected-sideways thesis is holding using
    existing Choppiness Index and Spike Monitor values from API state.
    """
    if active_spike:
        verdict = active_spike.get("verdict", "spike")
        return {"status": "BREAKOUT", "reason": f"active spike: {verdict}"}
    if choppiness is not None and choppiness < 38.2:
        return {"status": "TRENDING",
                "reason": f"choppiness {choppiness:.0f} — directional, not ranging"}
    if choppiness is not None:
        return {"status": "RANGING",
                "reason": f"choppiness {choppiness:.0f} — consistent with sideways thesis"}
    return {"status": "RANGING", "reason": "choppiness unavailable — assume ranging"}

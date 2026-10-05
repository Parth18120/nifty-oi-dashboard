"""Entry rules: sweet-zone strikes, iron-condor feasibility, and the can_enter gate."""

from datetime import datetime

from oi_dashboard.core import state
from oi_dashboard.core.constants import IST
from oi_dashboard.trading.activity_log import log_auto


def sweet_zone(dte):
    """Return (min_otm, max_otm) for sell legs based on DTE. Never returns closer than DANGER_ZONE_MAX_OTM."""
    from config import DANGER_ZONE_MAX_OTM, SWEET_ZONE_BY_DTE
    zone = SWEET_ZONE_BY_DTE.get(max(1, min(7, dte)), (400, 600))
    return (max(zone[0], DANGER_ZONE_MAX_OTM), zone[1])


def pick_directional_trade(mode):
    """Return PUT_SPREAD or CALL_SPREAD for a given directional classifier mode."""
    if mode in ("GAP_UP", "REVERSAL_DOWN"):
        return "PUT_SPREAD"   # sell PE spread
    if mode in ("GAP_DOWN", "REVERSAL_UP"):
        return "CALL_SPREAD"  # sell CE spread
    return None


def check_ic_feasibility(chain_out, spot, dte):
    """
    True only if BOTH CE and PE sides have a qualifying sell strike in the sweet zone:
      - strike OTM distance within [otm_min, otm_max]
      - LTP >= 8
      - ce_rv_ovr / pe_rv_ovr > IC_MIN_OVR_PCT (positive OVR required)
    Logs result to _auto_activity_log regardless.
    """
    try:
        from config import IC_MIN_OVR_PCT
    except Exception:
        IC_MIN_OVR_PCT = 0.0

    try:
        from config import MIN_LTP as _IC_MIN_LTP
    except ImportError:
        _IC_MIN_LTP = 15.0
    otm_min, otm_max = sweet_zone(dte)

    print(f"[IC PRE-CHECK] spot={spot:.1f} DTE={dte} sweet_zone={otm_min}-{otm_max}pt")
    for row in chain_out:
        s      = row["strike"]
        ce_otm = s - spot
        pe_otm = spot - s
        ce_ltp = float(row.get("ce_ltp") or 0)
        pe_ltp = float(row.get("pe_ltp") or 0)
        ce_ovr = row.get("ce_rv_ovr")
        pe_ovr = row.get("pe_rv_ovr")
        in_ce  = otm_min <= ce_otm <= otm_max
        in_pe  = otm_min <= pe_otm <= otm_max
        if in_ce or in_pe:
            ce_flag = f"CE_OTM={ce_otm:.0f} LTP={ce_ltp:.1f} OVR={ce_ovr}" if in_ce else ""
            pe_flag = f"PE_OTM={pe_otm:.0f} LTP={pe_ltp:.1f} OVR={pe_ovr}" if in_pe else ""
            ce_pass = in_ce and ce_ltp >= 8 and ce_ovr is not None and ce_ovr > IC_MIN_OVR_PCT
            pe_pass = in_pe and pe_ltp >= 8 and pe_ovr is not None and pe_ovr > IC_MIN_OVR_PCT
            status  = ("CE:PASS" if ce_pass else ("CE:FAIL" if in_ce else "")) + \
                      (" " if in_ce and in_pe else "") + \
                      ("PE:PASS" if pe_pass else ("PE:FAIL" if in_pe else ""))
            print(f"[IC PRE-CHECK]   strike={s}  {ce_flag}  {pe_flag}  → {status.strip()}")

    ce_ok      = False
    pe_ok      = False
    ce_detail  = f"no strike in {otm_min}-{otm_max}pt zone with LTP>={_IC_MIN_LTP} and OVR>{IC_MIN_OVR_PCT}%"
    pe_detail  = f"no strike in {otm_min}-{otm_max}pt zone with LTP>={_IC_MIN_LTP} and OVR>{IC_MIN_OVR_PCT}%"

    for row in chain_out:
        s      = row["strike"]
        ce_otm = s - spot
        pe_otm = spot - s
        ce_ovr = row.get("ce_rv_ovr")
        pe_ovr = row.get("pe_rv_ovr")
        ce_ltp = float(row.get("ce_ltp") or 0)
        pe_ltp = float(row.get("pe_ltp") or 0)

        if not ce_ok and otm_min <= ce_otm <= otm_max and ce_ltp >= _IC_MIN_LTP:
            if ce_ovr is not None and ce_ovr > IC_MIN_OVR_PCT:
                ce_ok     = True
                ce_detail = f"{s} CE OVR={ce_ovr:.1f}% LTP={ce_ltp:.2f}"
            else:
                ce_detail = f"{s} CE OVR={ce_ovr} (need >{IC_MIN_OVR_PCT}%)"

        if not pe_ok and otm_min <= pe_otm <= otm_max and pe_ltp >= _IC_MIN_LTP:
            if pe_ovr is not None and pe_ovr > IC_MIN_OVR_PCT:
                pe_ok     = True
                pe_detail = f"{s} PE OVR={pe_ovr:.1f}% LTP={pe_ltp:.2f}"
            else:
                pe_detail = f"{s} PE OVR={pe_ovr} (need >{IC_MIN_OVR_PCT}%)"

    result = ce_ok and pe_ok
    status = "FEASIBLE" if result else "INFEASIBLE"
    log_msg = (f"[IC FEASIBILITY] {status} | "
               f"CE={'OK: ' + ce_detail if ce_ok else 'NO: ' + ce_detail} | "
               f"PE={'OK: ' + pe_detail if pe_ok else 'NO: ' + pe_detail}")
    print(log_msg)
    log_auto("IC_FEASIBILITY", log_msg)
    return result


def can_enter(trade_type, direction=None):
    """
    Regime-based entry gate. Allows multiple entries per day as long as:
      - No OPEN trade of the same type already exists
      - Same type wasn't STOPPED/KILLED within the last 30 min (prevents martingale re-entry)
      - For GAP_SPREAD: same direction wasn't already closed/stopped today (no doubling down)
      - For PUT_SPREAD/CALL_SPREAD: only one directional trade per day across all directional types
    PANIC_BUY is exempt — it's a defensive conversion, not a new position.
    """
    if trade_type == "PANIC_BUY":
        return True, "OK"

    # Block if an OPEN trade of this type already exists
    open_of_type = [t for t in state.paper_trades
                    if t.get("status") == "OPEN" and t.get("trade_type") == trade_type]
    if open_of_type:
        return False, f"OPEN {trade_type} already exists (id #{open_of_type[0]['id']})"

    # PUT_SPREAD / CALL_SPREAD: one directional trade per day across all directional types
    _DIRECTIONAL = {"GAP_SPREAD", "PUT_SPREAD", "CALL_SPREAD"}
    if trade_type in _DIRECTIONAL:
        open_dir = [t for t in state.paper_trades
                    if t.get("trade_type") in _DIRECTIONAL and t.get("status") == "OPEN"]
        if open_dir:
            return False, (f"Directional trade #{open_dir[0]['id']} "
                           f"({open_dir[0]['trade_type']}) already OPEN — one per day")
        any_dir = [t for t in state.paper_trades if t.get("trade_type") in _DIRECTIONAL]
        if any_dir:
            return False, (f"Directional trade already ran today "
                           f"(#{any_dir[0]['id']} {any_dir[0]['trade_type']}) — no re-entry")

    # 30-min cooldown after a stop or kill
    now = datetime.now(IST)
    today_str = now.strftime("%Y-%m-%d")
    def _exit_dt(t):
        et = t.get("exit_time") or ""
        if not et:
            return None
        try:
            return datetime.strptime(f"{today_str} {et[:8]}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
        except Exception:
            return None

    recently_stopped = [
        t for t in state.paper_trades
        if t.get("trade_type") == trade_type
        and t.get("status") in ("STOPPED", "KILLED")
        and _exit_dt(t) is not None
        and (now - _exit_dt(t)).total_seconds() < 1800
    ]
    if recently_stopped:
        mins = int((now - _exit_dt(recently_stopped[0])).total_seconds() // 60)
        return False, f"{trade_type} stopped {mins}min ago — 30min cooldown"

    # GAP_SPREAD: same-direction lock for the whole day (no martingale on failing thesis)
    if trade_type == "GAP_SPREAD" and direction:
        same_dir_closed = [
            t for t in state.paper_trades
            if t.get("trade_type") == "GAP_SPREAD"
            and t.get("direction") == direction
            and t.get("status") in ("STOPPED", "KILLED", "EOD", "TARGET", "GAP_REVERSAL_EXIT", "IV_CRUSH")
        ]
        if same_dir_closed:
            return False, f"{direction} already closed today — no same-direction re-entry"

    return True, "OK"

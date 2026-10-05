"""Daily IV-crush tracking and the IV-crush exit rule for open trades."""

from datetime import datetime

from oi_dashboard.analytics.pricing import get_atm_iv
from oi_dashboard.core import state
from oi_dashboard.core.constants import IST, LOT_SIZE
from oi_dashboard.storage.session_store import save_iv_crush_log
from oi_dashboard.trading.fills import fetch_fresh_exit_prices


def try_iv_crush_snapshot(atm_iv, spot, now_ist_obj):
    if not atm_iv or atm_iv <= 0:
        return
    mkt_min = now_ist_obj.hour * 60 + now_ist_obj.minute
    today   = now_ist_obj.strftime("%Y-%m-%d")

    # 9:20am snapshot — capture open IV
    if 9*60+18 <= mkt_min <= 9*60+25 and not state.iv_crush_today_snap.get("open_iv"):
        state.iv_crush_today_snap = {"open_iv": round(atm_iv, 2), "open_spot": spot, "date": today}
        print(f"[IV] Open snapshot: IV={atm_iv:.2f}% spot={spot}")

    # 3:20pm snapshot — compute crush and append
    if mkt_min >= 15*60+18 and state.iv_crush_today_snap.get("open_iv") and not state.iv_crush_today_snap.get("close_logged"):
        open_iv  = state.iv_crush_today_snap["open_iv"]
        close_iv = round(atm_iv, 2)
        crush    = round((open_iv - close_iv) / open_iv * 100, 1) if open_iv else 0
        # Remove existing entry for today, add updated one
        state.iv_crush_log = [r for r in state.iv_crush_log if r.get("date") != today]
        state.iv_crush_log.append({
            "date":       today,
            "open_iv":    open_iv,
            "close_iv":   close_iv,
            "crush_pct":  crush,
            "spot_open":  state.iv_crush_today_snap.get("open_spot", 0),
            "spot_close": spot,
            "spot_range": round(abs(spot - state.iv_crush_today_snap.get("open_spot", spot)), 1),
        })
        state.iv_crush_today_snap["close_logged"] = True
        save_iv_crush_log()
        print(f"[IV] Crush logged: {open_iv:.1f}% → {close_iv:.1f}% = {crush:.1f}% crush")


def check_iv_crush_exit(trade, chain_out, spot, time_str, pnl_unit, lots):
    """
    IV-crush exit with sanity gates.
    Fires when 70%+ of the entry IV-premium has been captured, subject to:
      - Minimum 30-min hold
      - Entry IV in 8–25% range
      - IV premium > 1 pp above RV20 at entry
      - Current IV in 5–30% range
    Logs every check to stdout.
    """
    entry_iv  = float(trade.get("entry_atm_iv") or 0)
    entry_rv  = float(trade.get("entry_rv20")   or 0)
    trade_id  = trade.get("id", "?")
    tt        = trade.get("trade_type", "?")

    if entry_iv <= 0:
        trade["iv_capture_pct"] = None
        return False

    # Gate 1: entry IV must be 8–25%
    if not (8 <= entry_iv <= 25):
        print(f"[IV CRUSH] #{trade_id} {tt}: SKIP entry_iv={entry_iv:.1f}% not in [8,25]")
        trade["iv_capture_pct"] = None
        return False

    # Gate 2: IV premium > 1 pp above RV20
    iv_premium = entry_iv - entry_rv
    if iv_premium <= 1.0:
        print(f"[IV CRUSH] #{trade_id} {tt}: SKIP iv_premium={iv_premium:.2f}pp <=1pp")
        trade["iv_capture_pct"] = None
        return False

    # Gate 3: minimum 30-min hold
    entry_time_str = trade.get("entry_time", "")
    if entry_time_str:
        try:
            from datetime import datetime as _dt
            now_ist    = datetime.now(IST)
            today_date = now_ist.strftime("%Y-%m-%d")
            entry_dt   = IST.localize(_dt.strptime(f"{today_date} {entry_time_str}", "%Y-%m-%d %H:%M:%S"))
            held_min   = (now_ist - entry_dt).total_seconds() / 60
            if held_min < 30:
                print(f"[IV CRUSH] #{trade_id} {tt}: SKIP held={held_min:.0f}min < 30min")
                trade["iv_capture_pct"] = None
                return False
        except Exception:
            pass

    current_iv = get_atm_iv(chain_out, spot)
    if current_iv <= 0:
        return False

    # Gate 4: current IV must be 5–30%
    if not (5 <= current_iv <= 30):
        print(f"[IV CRUSH] #{trade_id} {tt}: SKIP current_iv={current_iv:.1f}% not in [5,30]")
        trade["iv_capture_pct"] = None
        return False

    iv_captured = entry_iv - current_iv
    capture_pct = round(iv_captured / iv_premium * 100, 1)
    trade["iv_capture_pct"] = capture_pct

    print(f"[IV CRUSH] #{trade_id} {tt}: entry_iv={entry_iv:.1f}% cur_iv={current_iv:.1f}% "
          f"rv20={entry_rv:.1f}% premium={iv_premium:.1f}pp captured={capture_pct:.0f}%")

    if capture_pct >= 70:
        fetch_fresh_exit_prices(trade)
        trade.update(
            status      = "IV_CRUSH",
            exit_time   = time_str,
            exit_reason = "IV_CRUSH_CAPTURED",
            pnl_per_lot = round(pnl_unit * LOT_SIZE, 2),
            pnl_total   = round(pnl_unit * LOT_SIZE * lots, 2),
        )
        print(f"[IV CRUSH] #{trade_id} EXIT captured={capture_pct:.0f}%  pnl=₹{trade['pnl_total']:.0f}")
        return True
    return False

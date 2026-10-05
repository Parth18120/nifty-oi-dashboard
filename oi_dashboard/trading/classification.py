"""Market-condition classification (gap / range / trend) and gap-confidence scoring."""

from datetime import datetime

from oi_dashboard.analytics.intraday import compute_choppiness, compute_rolling_rv, get_spot_range
from oi_dashboard.core import state
from oi_dashboard.core.constants import IST
from oi_dashboard.storage.session_store import save_gap_confidence_log


def check_path_conditions():
    """
    Path-awareness gate for AUTO entries. Returns (ok, reason).
    Called before IC feasibility check in _try_auto_enter().
    """
    chop, chop_label = compute_choppiness(30)
    rv_now           = compute_rolling_rv(15)
    now_ist          = datetime.now(IST)

    # Rule 1: block only on EXTREME_CHOP (> 75 on Dreiss 0-100 scale)
    if chop is not None and chop > 75.0:
        return False, f"chop {chop:.1f} EXTREME_CHOP — skip"

    # Rule 2: CHOPPY zone (61.8-75) + extreme intraday RV → block
    if chop is not None and chop > 61.8 and rv_now is not None and rv_now > 30.0:
        return False, f"chop {chop:.1f} CHOPPY + RV {rv_now:.1f}% high — skip"

    # Rule 3: no entry within 15 min of a micro move
    if state.micro_moves_today:
        last_t     = state.micro_moves_today[-1]["time"]
        secs_since = (now_ist - last_t).total_seconds()
        if secs_since < 900:
            return False, f"micro move {secs_since/60:.0f}min ago — wait 15min"

    # Rule 4: standalone extreme intraday RV
    if rv_now is not None and rv_now > 30.0:
        return False, f"rolling RV {rv_now:.1f}% extreme — wait"

    chop_str = f"chop {chop:.1f} {chop_label}" if chop is not None else "no chop data"
    return True, f"path OK ({chop_str})"


def detect_gap(spot_data):
    """Returns gap % vs previous close. Positive=gap up, Negative=gap down."""
    prev = float(spot_data.get("prev_close", 0) or 0)
    opn  = float(spot_data.get("open",       0) or 0)
    if prev <= 0 or opn <= 0:
        return 0.0
    return round((opn - prev) / prev * 100, 3)


def classify_market(spot_data, chain_out, spot, vix=0.0, pcr=1.0):
    """
    Called at 9:30am. Returns one of:
      'GAP_UP', 'GAP_DOWN'   — directional, score >= 75
      'IRON_CONDOR'           — gap day but score 50-74, or gap < 0.3%
      'OVERPRICED'            — OVR > 30% found
      'RANGE'                 — 60-min range < 150pt
      'SKIP'                  — nothing qualifies
    """
    gap_pct    = detect_gap(spot_data)
    rng        = get_spot_range(60)
    prev_close = float(spot_data.get("prev_close", 0) or 0)
    open_price = float(spot_data.get("open", 0) or 0)    # 9:15 open = candle_open

    if abs(gap_pct) > 0.3:
        direction = "GAP_UP" if gap_pct > 0 else "GAP_DOWN"
        score, reasons = gap_confidence_score(
            direction,
            spot_930     = spot,
            candle_open  = open_price,
            candle_close = spot,       # 9:30 spot ≈ 15-min candle close
            prev_close   = prev_close,
            vix_now      = vix,
            vix_prev     = state.vix_at_open,
            pcr          = pcr,
        )
        # Decision
        if score >= 75:
            final = direction          # GAP_UP or GAP_DOWN → directional spread
        elif score >= 50:
            final = "IRON_CONDOR"
        else:
            final = "SKIP"

        caption = f"gap {gap_pct:+.2f}% · confidence {score}% · {final.replace('_',' ').lower()}"
        state.last_mode_reason   = caption
        state.last_gap_confidence = {
            "date":       datetime.now(IST).strftime("%Y-%m-%d"),
            "direction":  direction,
            "gap_pct":    round(gap_pct, 3),
            "score":      score,
            "reasons":    reasons,
            "final_mode": final,
        }
        reasons_str = ", ".join(reasons)
        print(f"[MODE] 09:30 direction={direction} confidence={score}%")
        print(f"  reasons: {reasons_str}")
        print(f"  → {final}")
        save_gap_confidence_log(state.last_gap_confidence)
        return final

    for row in chain_out:
        if float(row.get("pe_rv_ovr") or 0) > 30 or float(row.get("ce_rv_ovr") or 0) > 30:
            state.last_mode_reason    = "OVR>30% found"
            state.last_gap_confidence = {}
            print(f"[MODE] OVERPRICED: OVR found — iron condor")
            return "OVERPRICED"

    if rng < 150:
        state.last_mode_reason    = f"{rng:.0f}pt range"
        state.last_gap_confidence = {}
        print(f"[MODE] RANGE: {rng:.0f}pt range — naked strangle")
        return "RANGE"

    state.last_mode_reason    = f"gap={gap_pct:+.2f}% range={rng:.0f}pt no signal"
    state.last_gap_confidence = {}
    print(f"[MODE] SKIP: gap={gap_pct:+.2f}% range={rng:.0f}pt no OVR")
    return "SKIP"


def gap_confidence_score(direction, spot_930, candle_open, candle_close,
                          prev_close, vix_now, vix_prev, pcr):
    """
    4-factor confidence check for directional gap trades.
    Returns (score 0-100, reasons list).
    score >= 75 → allow GAP_SPREAD
    score 50-74 → force IRON_CONDOR
    score < 50  → SKIP
    """
    score   = 0
    reasons = []

    # Factor 1 (40%): 15-min candle direction — candle_open=9:15 open, candle_close=9:30 spot
    if candle_open and candle_close:
        if direction == "GAP_UP":
            if candle_close > candle_open:
                score += 40; reasons.append("15min green ✓")
            else:
                reasons.append("15min red ✗ (reversal warning)")
        else:
            if candle_close < candle_open:
                score += 40; reasons.append("15min red ✓")
            else:
                reasons.append("15min green ✗ (reversal warning)")
    else:
        reasons.append("15min candle N/A")

    # Factor 2 (25%): spot vs prev_close
    if prev_close and spot_930:
        if direction == "GAP_UP":
            if spot_930 > prev_close:
                score += 25; reasons.append("above prev close ✓")
            else:
                reasons.append("below prev close ✗ (gap filled)")
        else:
            if spot_930 < prev_close:
                score += 25; reasons.append("below prev close ✓")
            else:
                reasons.append("above prev close ✗ (gap filled)")
    else:
        reasons.append("prev_close N/A")

    # Factor 3 (20%): VIX rising >= 3% vs open VIX
    if vix_prev and vix_prev > 0:
        vix_chg = (vix_now - vix_prev) / vix_prev
        if vix_chg > 0.03:
            score += 20; reasons.append(f"VIX +{vix_chg*100:.1f}% ✓")
        else:
            reasons.append(f"VIX {vix_chg*100:+.1f}% ✗ (weak gap)")
    else:
        reasons.append("VIX prev N/A")

    # Factor 4 (15%): PCR alignment
    if pcr and pcr > 0:
        if direction == "GAP_UP":
            if pcr < 1.1:
                score += 15; reasons.append(f"PCR {pcr:.2f} ✓")
            else:
                reasons.append(f"PCR {pcr:.2f} ✗ (put-heavy, bounce risk)")
        else:
            if pcr > 0.9:
                score += 15; reasons.append(f"PCR {pcr:.2f} ✓")
            else:
                reasons.append(f"PCR {pcr:.2f} ✗ (call-heavy, reversal risk)")
    else:
        reasons.append("PCR N/A")

    return score, reasons

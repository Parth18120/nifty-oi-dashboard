"""
PE / CE Activity Classifier

Classifies WHY PE or CE OI/IV is changing today.

Three archetypes with opposite trading implications:
    FEAR         — retail/institutional buying puts (crash fear). DO NOT sell puts.
    WRITING      — institutions selling puts for income premium. SAFE to sell puts.
    HEDGE_SPREAD — institutions selling ATM + buying OTM (margin reduction).
                   Skew signal is inflated. Sell PE spread with caution.

Key field used: pe_chg_from_open / ce_chg_from_open — already computed in
chain_out by market.snapshot using session-start OI snapshot (not last-refresh delta).
"""

import json
import os
import time as _time
from datetime import datetime, timedelta, timezone

from oi_dashboard.core.paths import ROOT

_LOG_PATH = os.path.join(str(ROOT),
                         "data", "pe_activity_log.json")

# ── Market open timestamp for vol/OI time-normalisation ──────────────────────
_IST = timezone(timedelta(hours=5, minutes=30))


def _hours_since_open():
    now = datetime.now(_IST)
    open_today = now.replace(hour=9, minute=15, second=0, microsecond=0)
    return max((now - open_today).total_seconds() / 3600, 0.5)


# ── Core OI-change helpers ────────────────────────────────────────────────────

def _pe_oi_chg_pct(rows, spot):
    """
    % change in PE OI from session start for strikes BELOW spot.
    Uses pe_chg_from_open (= pe_oi - open_oi) already computed in chain_out.
    Returns None when there is no session-start baseline yet (all zero).
    """
    pe = [r for r in rows if r["strike"] < spot]
    if not pe:
        return 0.0, False
    total_open = sum(r.get("pe_oi", 0) - r.get("pe_chg_from_open", 0) for r in pe)
    total_chg  = sum(r.get("pe_chg_from_open", 0) for r in pe)
    if total_open <= 0:
        return 0.0, False
    has_data = abs(total_chg) > 0
    return round(total_chg / total_open * 100, 1), has_data


def _ce_oi_chg_pct(rows, spot):
    ce = [r for r in rows if r["strike"] > spot]
    if not ce:
        return 0.0, False
    total_open = sum(r.get("ce_oi", 0) - r.get("ce_chg_from_open", 0) for r in ce)
    total_chg  = sum(r.get("ce_chg_from_open", 0) for r in ce)
    if total_open <= 0:
        return 0.0, False
    has_data = abs(total_chg) > 0
    return round(total_chg / total_open * 100, 1), has_data


def _vol_oi_ratio(rows, side):
    """
    Time-normalised vol/OI ratio for `side` ('pe' or 'ce').
    - Excludes strikes with OI < 10,000 (avoids div-by-near-zero explosions).
    - Divides by hours elapsed since open so ratio stays ~constant across the day.
    Returns None when no meaningful strikes available.
    """
    oi_key  = f"{side}_oi"
    vol_key = f"{side}_volume"
    meaningful = [r for r in rows if r.get(oi_key, 0) > 10_000]
    if not meaningful:
        return None
    ratios = []
    for r in meaningful:
        oi  = r.get(oi_key,  0)
        vol = r.get(vol_key, 0)
        if oi > 0:
            ratios.append(vol / oi)
    if not ratios:
        return None
    raw = sum(ratios) / len(ratios)
    return round(raw / _hours_since_open(), 3)   # normalise to per-hour-since-open


# ── Confidence: margin-aware ──────────────────────────────────────────────────

def _confidence(scores):
    """
    Combined dominance + margin.  Honest: tight races score low.
    Returns 0-100 float.
    """
    total = sum(scores.values())
    if total == 0:
        return 0.0
    vals       = sorted(scores.values(), reverse=True)
    winner     = vals[0]
    runner_up  = vals[1] if len(vals) > 1 else 0
    dominance  = winner / total * 100
    margin     = (winner - runner_up) / total * 100
    return round((dominance + margin) / 2, 1)


# ── PE classifier ─────────────────────────────────────────────────────────────

def classify_pe_activity(chain, spot, vix_now, vix_prev, spot_change_pct):
    """
    Classify the reason behind PE OI/IV increase.
    Returns: {type, confidence, scores, reasoning, buckets}
    """
    if not chain or not spot:
        return _unclear("no chain data")

    atm  = [r for r in chain if abs(r["strike"] - spot) <= 200]
    mid  = [r for r in chain if 200 < abs(r["strike"] - spot) <= 500]
    far  = [r for r in chain if 500 < abs(r["strike"] - spot) <= 900]
    deep = [r for r in chain if abs(r["strike"] - spot) > 900]

    atm_chg,  atm_has  = _pe_oi_chg_pct(atm,  spot)
    mid_chg,  mid_has  = _pe_oi_chg_pct(mid,  spot)
    far_chg,  far_has  = _pe_oi_chg_pct(far,  spot)
    deep_chg, deep_has = _pe_oi_chg_pct(deep, spot)

    oi_has_data = any([atm_has, mid_has, far_has, deep_has])

    # ATM PE strikes for vol/OI ratio
    atm_pe  = [r for r in atm if r["strike"] < spot]
    pe_voi  = _vol_oi_ratio(atm_pe, "pe")

    vix_chg = ((vix_now - vix_prev) / vix_prev * 100) if (vix_prev and vix_prev > 0) else 0

    # ── Sanity gate: if no OI data AND mkt is quiet, return UNCLEAR ──────────
    if not oi_has_data and abs(spot_change_pct) < 0.5 and abs(vix_chg) < 3:
        return _unclear("insufficient OI delta data and market too quiet to classify")

    signals   = {"FEAR": 0, "WRITING": 0, "HEDGE_SPREAD": 0}
    reasoning = []

    # Rule 1: spot direction
    if spot_change_pct < -0.3:
        signals["FEAR"] += 3
        reasoning.append(f"spot down {spot_change_pct:.2f}% (bearish)")
    elif spot_change_pct > 0.3:
        signals["WRITING"]      += 2
        signals["HEDGE_SPREAD"] += 2
        reasoning.append(f"spot up {spot_change_pct:+.2f}% (bullish — not fear)")
    else:
        signals["WRITING"] += 1
        reasoning.append(f"spot flat ({spot_change_pct:+.2f}%)")

    # Rule 2: VIX
    if vix_chg > 15:
        signals["FEAR"] += 3
        reasoning.append(f"VIX +{vix_chg:.1f}% (fear spike)")
    elif vix_chg > 5:
        signals["FEAR"] += 1
        reasoning.append(f"VIX +{vix_chg:.1f}% (mild concern)")
    else:
        signals["WRITING"]      += 1
        signals["HEDGE_SPREAD"] += 1
        reasoning.append(f"VIX {vix_chg:+.1f}% (calm)")

    # Rule 3: OI distribution (only scored when data exists)
    if oi_has_data:
        if far_chg > 100 and deep_chg > 50:
            signals["HEDGE_SPREAD"] += 3
            reasoning.append(f"far OTM PE OI +{far_chg:.0f}%, deep +{deep_chg:.0f}% (tail hedging)")
        elif atm_chg > 100 and far_chg < 30:
            signals["WRITING"] += 2
            reasoning.append(f"ATM PE OI +{atm_chg:.0f}%, far only +{far_chg:.0f}% (income writing)")
        elif atm_chg > 100 and mid_chg > 50:
            signals["FEAR"]    += 1
            signals["WRITING"] += 1
            reasoning.append(f"broad PE OI build ATM+mid (mixed)")
        else:
            reasoning.append(f"PE OI pattern unclear "
                             f"(atm={atm_chg:+.0f}% mid={mid_chg:+.0f}% far={far_chg:+.0f}%)")
    else:
        reasoning.append("PE OI delta not yet accumulated (session too fresh or flat day)")

    # Rule 4: vol/OI ratio (time-normalised)
    if pe_voi is not None:
        if pe_voi > 0.5:
            signals["FEAR"] += 1
            reasoning.append(f"PE vol/OI ratio {pe_voi:.2f}/hr (fresh activity)")
        elif pe_voi < 0.15:
            signals["HEDGE_SPREAD"] += 1
            reasoning.append(f"PE vol/OI ratio {pe_voi:.2f}/hr (institutional sticky)")
        else:
            reasoning.append(f"PE vol/OI ratio {pe_voi:.2f}/hr (neutral)")

    conf   = _confidence(signals)
    winner = max(signals, key=signals.get)
    if conf < 40:
        winner = "UNCLEAR"

    return {
        "type":       winner,
        "confidence": conf,
        "scores":     signals,
        "reasoning":  reasoning,
        "buckets": {
            "atm_pe_chg_pct":   atm_chg,
            "mid_pe_chg_pct":   mid_chg,
            "far_pe_chg_pct":   far_chg,
            "deep_pe_chg_pct":  deep_chg,
            "pe_vol_oi_ratio":  pe_voi,
            "vix_change_pct":   round(vix_chg, 1),
            "spot_change_pct":  round(spot_change_pct, 2),
            "oi_has_data":      oi_has_data,
        },
    }


# ── CE classifier ─────────────────────────────────────────────────────────────

def classify_ce_activity(chain, spot, vix_now, vix_prev, spot_change_pct):
    """
    Mirror of classify_pe_activity for CE (call) side.
    FEAR = rally-chasing (spot UP hard + VIX rising). DO NOT sell calls.
    """
    if not chain or not spot:
        return _unclear("no chain data")

    atm  = [r for r in chain if abs(r["strike"] - spot) <= 200]
    mid  = [r for r in chain if 200 < abs(r["strike"] - spot) <= 500]
    far  = [r for r in chain if 500 < abs(r["strike"] - spot) <= 900]
    deep = [r for r in chain if abs(r["strike"] - spot) > 900]

    atm_chg,  atm_has  = _ce_oi_chg_pct(atm,  spot)
    mid_chg,  mid_has  = _ce_oi_chg_pct(mid,  spot)
    far_chg,  far_has  = _ce_oi_chg_pct(far,  spot)
    deep_chg, deep_has = _ce_oi_chg_pct(deep, spot)

    oi_has_data = any([atm_has, mid_has, far_has, deep_has])

    atm_ce  = [r for r in atm if r["strike"] > spot]
    ce_voi  = _vol_oi_ratio(atm_ce, "ce")

    vix_chg = ((vix_now - vix_prev) / vix_prev * 100) if (vix_prev and vix_prev > 0) else 0

    # CE FEAR requires BOTH: spot up AND VIX up (gap-up anxiety).
    # Without both, market is calm — return UNCLEAR if data insufficient.
    if not oi_has_data and abs(vix_chg) < 3:
        return _unclear("insufficient CE OI data and VIX calm")

    signals   = {"FEAR": 0, "WRITING": 0, "HEDGE_SPREAD": 0}
    reasoning = []

    # Rule 1: spot direction (CE fear = sharp upside rally-chase)
    if spot_change_pct > 0.5:
        signals["FEAR"] += 2          # only 2, not 3 — need VIX confirmation too
        reasoning.append(f"spot up {spot_change_pct:+.2f}% (potential rally-chasing)")
    elif spot_change_pct < -0.3:
        signals["WRITING"]      += 2
        signals["HEDGE_SPREAD"] += 2
        reasoning.append(f"spot down {spot_change_pct:.2f}% (down day — not CE fear)")
    else:
        signals["WRITING"] += 1
        reasoning.append(f"spot flat ({spot_change_pct:+.2f}%)")

    # Rule 2: VIX (CE FEAR requires VIX confirmation)
    if vix_chg > 10:
        signals["FEAR"] += 3          # real anxiety spike confirms rally-chase fear
        reasoning.append(f"VIX +{vix_chg:.1f}% (gap-up anxiety)")
    elif vix_chg > 5:
        signals["FEAR"] += 1
        reasoning.append(f"VIX +{vix_chg:.1f}% (mild)")
    else:
        signals["WRITING"]      += 1
        signals["HEDGE_SPREAD"] += 1
        reasoning.append(f"VIX {vix_chg:+.1f}% (calm — CE FEAR unlikely)")

    # Rule 3: CE OI distribution — spread-ratio logic disambiguates fear vs writing
    spread_ratio      = None
    spread_ratio_label = None
    if oi_has_data:
        far_ce_rising = far_chg > 30
        atm_ce_rising = atm_chg > 30

        if far_ce_rising and atm_ce_rising:
            # Both buckets rising: compute ratio to distinguish spread trade vs fear
            spread_ratio = round(far_chg / atm_chg, 2) if atm_chg > 0 else None

            if spread_ratio is None:
                reasoning.append(f"CE OI both rising but ATM=0 — cannot compute ratio")
                spread_ratio_label = "n/a"
            elif spread_ratio < 0.35:
                # ATM dominates far — institutions selling ATM, buying far as hedge leg
                signals["WRITING"] += 3
                spread_ratio_label = f"spread hedge ({spread_ratio:.2f} < 0.35 = writing)"
                reasoning.append(
                    f"[CE] Far CE OI +{far_chg:.1f}% vs ATM CE OI +{atm_chg:.1f}%"
                    f" — ratio {spread_ratio:.2f} = spread hedge legs, not fear buying"
                )
            elif spread_ratio < 0.70:
                # Mixed — some spread, some directional buying
                signals["WRITING"] += 1
                signals["FEAR"]    += 1
                spread_ratio_label = f"mixed ({spread_ratio:.2f}, 0.35-0.70)"
                reasoning.append(
                    f"[CE] Far CE OI +{far_chg:.1f}% vs ATM CE OI +{atm_chg:.1f}%"
                    f" — ratio {spread_ratio:.2f} = mixed (spread + some directional)"
                )
            else:
                # Far OI dominant — genuine upside fear buying
                signals["FEAR"] += 3
                spread_ratio_label = f"fear buying ({spread_ratio:.2f} >= 0.70)"
                reasoning.append(
                    f"[CE] Far CE OI +{far_chg:.1f}% vs ATM CE OI +{atm_chg:.1f}%"
                    f" — ratio {spread_ratio:.2f} = far dominates, genuine fear buying"
                )

        elif far_ce_rising and not atm_ce_rising:
            # Only far OI rising — pure fear buying with no writing component
            signals["FEAR"] += 3
            spread_ratio_label = "fear only (ATM flat/falling)"
            reasoning.append(
                f"[CE] Far CE OI +{far_chg:.1f}% with ATM CE flat/down ({atm_chg:+.1f}%)"
                f" — pure fear buying (no writing component)"
            )

        elif atm_chg > 100 and far_chg < 30:
            signals["WRITING"] += 2
            spread_ratio_label = "naked writing (far flat)"
            reasoning.append(f"ATM CE OI +{atm_chg:.0f}%, far only +{far_chg:.0f}% (naked call writing)")

        elif atm_chg > 100 and mid_chg > 50:
            signals["FEAR"]    += 1
            signals["WRITING"] += 1
            spread_ratio_label = "broad build (mixed)"
            reasoning.append(f"broad CE OI build (mixed)")

        else:
            spread_ratio_label = "unclear"
            reasoning.append(f"CE OI pattern unclear "
                             f"(atm={atm_chg:+.0f}% mid={mid_chg:+.0f}% far={far_chg:+.0f}%)")
    else:
        reasoning.append("CE OI delta not yet accumulated")

    # Rule 4: vol/OI ratio
    if ce_voi is not None:
        if ce_voi > 0.5:
            signals["FEAR"] += 1
            reasoning.append(f"CE vol/OI ratio {ce_voi:.2f}/hr (fresh activity)")
        elif ce_voi < 0.15:
            signals["HEDGE_SPREAD"] += 1
            reasoning.append(f"CE vol/OI ratio {ce_voi:.2f}/hr (institutional)")

    conf   = _confidence(signals)
    winner = max(signals, key=signals.get)
    if conf < 40:
        winner = "UNCLEAR"

    return {
        "type":       winner,
        "confidence": conf,
        "scores":     signals,
        "reasoning":  reasoning,
        "buckets": {
            "atm_ce_chg_pct":    atm_chg,
            "mid_ce_chg_pct":    mid_chg,
            "far_ce_chg_pct":    far_chg,
            "deep_ce_chg_pct":   deep_chg,
            "ce_vol_oi_ratio":   ce_voi,
            "vix_change_pct":    round(vix_chg, 1),
            "spot_change_pct":   round(spot_change_pct, 2),
            "oi_has_data":       oi_has_data,
            "ce_spread_ratio":   spread_ratio,
            "ce_spread_label":   spread_ratio_label,
        },
    }


# ── Spread Decay Analysis ─────────────────────────────────────────────────────

_STRIKE_STEP = 50

NEAR_THRESHOLD = 50_000
FAR_THRESHOLD  = 10_000


def _bucket_oi_changes(chain: list, spot: float) -> tuple:
    """
    Returns (near_ce, near_pe, far_ce, far_pe) total OI changes.
    near = ATM ± 200 pts
    far  = 500–900 pts OTM (inclusive on both ends)

    Primary source: ce_oi_chg / pe_oi_chg (Dhan raw OI change).
    Fallback: ce_chg_from_open / pe_chg_from_open (session-start delta).
    """
    atm = round(spot / _STRIKE_STEP) * _STRIKE_STEP
    near_ce = near_pe = far_ce = far_pe = 0

    for r in chain:
        dist  = r["strike"] - atm
        absd  = abs(dist)
        ce_chg = r.get("ce_oi_chg") or r.get("ce_chg_from_open") or 0
        pe_chg = r.get("pe_oi_chg") or r.get("pe_chg_from_open") or 0

        if absd <= 200:
            near_ce += ce_chg
            near_pe += pe_chg
        elif 500 <= absd <= 900:
            if dist > 0:   # OTM call side
                far_ce += ce_chg
            else:          # OTM put side
                far_pe += pe_chg

    return near_ce, near_pe, far_ce, far_pe


def _ce_conviction(near_ce: int, far_ce: int,
                   spot_chg: float) -> tuple:
    """
    CE spread conviction: call-side OI decay pattern.

    Returns (conviction_code, label).

    Market falling (spot_chg < -0.15%):
      near CE dying (< -50k): ratio = abs(far_ce)/abs(near_ce)
        ratio < 0.15  → BEARISH_HOLD (far very sticky — writers holding)
        ratio < 0.35  → PARTIAL_HOLD (some unwind but bulk stays)
        ratio >= 0.35 → CLOSING (both decaying, conviction fading)
      near CE positive + far CE positive:
        far/near < 0.35 → OPENING (new bearish spreads)
        else            → UPSIDE_FEAR (heavy far buying = squeeze hedge)

    Market rising (spot_chg > +0.15%):
      near CE > 0 + far CE > 0:
        far/near < 0.35 → NEW_SPREAD_WRITING (bearish against rally)
        else            → UPSIDE_FEAR (momentum/breakout buying)

    Flat: OPENING if building spreads, else NEUTRAL.
    """
    falling = spot_chg < -0.15
    rising  = spot_chg >  0.15

    if falling:
        if near_ce < -NEAR_THRESHOLD:
            ratio = abs(far_ce) / max(abs(near_ce), 1)
            if ratio < 0.15:
                return ("BEARISH_HOLD",
                        f"Near CE dying fast, far CE sticky "
                        f"(ratio {ratio:.2f}) — call writers holding position")
            elif ratio < 0.35:
                return ("PARTIAL_HOLD",
                        f"Call spreads partially maintained (ratio {ratio:.2f})")
            else:
                return ("CLOSING",
                        f"Both near and far CE decaying similarly "
                        f"(ratio {ratio:.2f}) — spreads being closed, conviction fading")

        if near_ce > 0 and far_ce > 0:
            ratio = far_ce / max(near_ce, 1)
            if ratio < 0.35:
                return ("OPENING",
                        f"New call spreads opening in falling market "
                        f"(ratio {ratio:.2f}) — fresh bearish positioning")
            return ("UPSIDE_FEAR",
                    "Heavy far CE buying in falling market — "
                    "upside hedge or squeeze protection")

    if rising:
        if near_ce > 0 and far_ce > 0:
            ratio = far_ce / max(near_ce, 1)
            if ratio < 0.35:
                return ("NEW_SPREAD_WRITING",
                        f"Call spread writing on rally "
                        f"(ratio {ratio:.2f}) — bearish against the move")
            return ("UPSIDE_FEAR",
                    "Strong far CE buying on rally — "
                    "momentum buying or breakout chase")

    # Flat market or no directional signal
    if near_ce > 0 and far_ce > 0 and far_ce < near_ce * 0.35:
        return ("OPENING",
                f"New call spreads building "
                f"(far/near={far_ce/max(near_ce,1):.2f}) — fresh bearish positioning")

    return ("NEUTRAL", "No clear CE spread pattern")


def _pe_conviction(near_pe: int, far_pe: int,
                   spot_chg: float) -> tuple:
    """
    PE spread conviction: put-side OI decay pattern. Mirror of _ce_conviction.

    Market rising (spot_chg > +0.15%):
      near PE dying (< -50k): ratio = abs(far_pe)/abs(near_pe)
        ratio < 0.15  → BULLISH_HOLD
        ratio < 0.35  → PARTIAL_HOLD
        ratio >= 0.35 → CLOSING
      near PE > 0 + far PE > 0:
        far/near < 0.35 → OPENING
        else            → DOWNSIDE_FEAR (hedge buying despite rising market)

    Market falling (spot_chg < -0.15%):
      near PE > 0 + far PE > 0:
        far/near < 0.35 → NEW_SPREAD_WRITING (bullish against drop)
        else            → DOWNSIDE_FEAR (genuine crash fear)

    Flat: OPENING if building spreads, else NEUTRAL.
    """
    rising  = spot_chg >  0.15
    falling = spot_chg < -0.15

    if rising:
        if near_pe < -NEAR_THRESHOLD:
            ratio = abs(far_pe) / max(abs(near_pe), 1)
            if ratio < 0.15:
                return ("BULLISH_HOLD",
                        f"Near PE dying fast, far PE sticky "
                        f"(ratio {ratio:.2f}) — put sellers holding position")
            elif ratio < 0.35:
                return ("PARTIAL_HOLD",
                        f"Put spreads partially maintained (ratio {ratio:.2f})")
            else:
                return ("CLOSING",
                        f"Both near and far PE decaying similarly "
                        f"(ratio {ratio:.2f}) — spreads being closed")

        if near_pe > 0 and far_pe > 0:
            ratio = far_pe / max(near_pe, 1)
            if ratio < 0.35:
                return ("OPENING",
                        f"New put spreads opening on rally "
                        f"(ratio {ratio:.2f}) — fresh bullish/neutral positioning")
            return ("DOWNSIDE_FEAR",
                    "Heavy far PE buying on rally — put protection buying "
                    "despite rising market (hedging longs)")

    if falling:
        if near_pe > 0 and far_pe > 0:
            ratio = far_pe / max(near_pe, 1)
            if ratio < 0.35:
                return ("NEW_SPREAD_WRITING",
                        f"Put spread writing on drop "
                        f"(ratio {ratio:.2f}) — bullish against the move")
            return ("DOWNSIDE_FEAR",
                    "Heavy far PE buying in falling market — "
                    "genuine crash fear, protective put buying")

    # Flat market or no directional signal
    if near_pe > 0 and far_pe > 0 and far_pe < near_pe * 0.35:
        return ("OPENING",
                f"New put spreads building "
                f"(far/near={far_pe/max(near_pe,1):.2f}) — fresh bullish/neutral positioning")

    return ("NEUTRAL", "No clear PE spread pattern")


def _combined_conviction(ce_conv: str, pe_conv: str,
                          spot_chg: float) -> tuple:
    """
    Most important output — what both sides together say.

    Returns (combined_signal, combined_label).
    """
    falling = spot_chg < -0.15
    rising  = spot_chg >  0.15

    if falling:
        if ce_conv == "BEARISH_HOLD" and pe_conv == "DOWNSIDE_FEAR":
            return ("STRONG_BEARISH",
                    "Call writers holding + genuine put fear = "
                    "strong bearish conviction. Do NOT sell puts.")
        if ce_conv == "BEARISH_HOLD":
            return ("BEARISH_HOLD",
                    "Call spread holders maintaining position. "
                    "Market likely continues weak or sideways.")
        if pe_conv == "DOWNSIDE_FEAR":
            return ("FEAR_DRIVEN_DOWN",
                    "Genuine protective put buying. "
                    "Do NOT sell puts — real downside risk present.")
        if ce_conv == "OPENING":
            return ("FRESH_BEARISH",
                    "New bearish positions being opened. "
                    "Trend continuation likely.")
        if ce_conv == "CLOSING" and pe_conv == "CLOSING":
            return ("POSITION_UNWIND",
                    "Both sides closing. Directional move may be "
                    "exhausting. Watch for reversal.")

    if rising:
        if pe_conv == "BULLISH_HOLD" and ce_conv == "UPSIDE_FEAR":
            return ("STRONG_BULLISH",
                    "Put sellers holding + upside hedging = "
                    "strong bullish conviction. Do NOT sell calls.")
        if pe_conv == "BULLISH_HOLD":
            return ("BULLISH_HOLD",
                    "Put spread holders maintaining position. "
                    "Market likely continues up.")
        if ce_conv == "UPSIDE_FEAR":
            return ("FEAR_DRIVEN_UP",
                    "Upside fear buying. Squeeze risk present. "
                    "Do NOT sell calls into this.")
        if pe_conv == "CLOSING" and ce_conv == "CLOSING":
            return ("POSITION_UNWIND",
                    "Both sides closing. Rally may be exhausting.")
        if pe_conv == "OPENING":
            return ("FRESH_BULLISH",
                    "New bullish positions being opened. "
                    "Rally continuation likely.")

    # Flat market
    if ce_conv == "OPENING":
        return ("FRESH_BEARISH_SETUP",
                "Bearish spreads being built in quiet market. "
                "Watch for downside break.")
    if pe_conv == "OPENING":
        return ("FRESH_BULLISH_SETUP",
                "Bullish spreads being built in quiet market. "
                "Watch for upside break.")

    return ("NEUTRAL", "No clear combined signal from spread activity.")


def analyze_spread_decay(chain: list, spot: float,
                          spot_change_pct: float,
                          now_ist=None) -> dict:
    """
    Analyze spread holder conviction from near vs far OI decay patterns.

    spot_change_pct: today's spot % change (e.g. -0.24 for -0.24%).
    now_ist: datetime in IST timezone. If None, computed internally.
    Runs alongside (not replacing) the existing FEAR/WRITING/UNCLEAR classification.

    Returns dict with conviction codes, labels, bucket totals, and stability fields.
    """
    import pytz as _pytz
    _IST_tz = _pytz.timezone('Asia/Kolkata')
    if now_ist is None:
        from datetime import datetime as _dt
        now_ist = _dt.now(_IST_tz)

    hour   = now_ist.hour
    minute = now_ist.minute

    # ── GATE 1: Opening auction noise (before 9:35 AM) ───────────────────────
    if hour == 9 and minute < 35:
        return {
            "combined_signal":     "OPENING_AUCTION",
            "combined_label":      "Opening auction active — OI changes unreliable before 9:35 AM.",
            "ce_conviction":       "NEUTRAL",
            "ce_conviction_label": "Wait for opening auction to settle",
            "pe_conviction":       "NEUTRAL",
            "pe_conviction_label": "Wait for opening auction to settle",
            "near_ce": 0, "far_ce": 0, "near_pe": 0, "far_pe": 0,
            "reasoning": [],
            "gated":      True,
            "gate_reason": "OPENING_AUCTION",
        }

    # ── GATE 2: End-of-day position squaring (after 14:30) ───────────────────
    if hour > 14 or (hour == 14 and minute >= 30):
        return {
            "combined_signal":     "END_OF_DAY",
            "combined_label":      "Last hour — position squaring noise. Signals unreliable after 14:30.",
            "ce_conviction":       "NEUTRAL",
            "ce_conviction_label": "End-of-day squaring distorts OI flows",
            "pe_conviction":       "NEUTRAL",
            "pe_conviction_label": "End-of-day squaring distorts OI flows",
            "near_ce": 0, "far_ce": 0, "near_pe": 0, "far_pe": 0,
            "reasoning": [],
            "gated":      True,
            "gate_reason": "END_OF_DAY",
        }

    if not chain or not spot:
        return {
            "ce_conviction": "NEUTRAL", "ce_conviction_label": "no chain data",
            "pe_conviction": "NEUTRAL", "pe_conviction_label": "no chain data",
            "combined_signal": "NEUTRAL", "combined_label": "no chain data",
            "near_ce": 0, "far_ce": 0, "near_pe": 0, "far_pe": 0,
            "reasoning": [],
            "gated": False,
        }

    near_ce, near_pe, far_ce, far_pe = _bucket_oi_changes(chain, spot)

    ce_conv,   ce_label   = _ce_conviction(near_ce, far_ce, spot_change_pct)
    pe_conv,   pe_label   = _pe_conviction(near_pe, far_pe, spot_change_pct)
    comb_conv, comb_label = _combined_conviction(ce_conv, pe_conv, spot_change_pct)

    reasoning = []
    if ce_conv != "NEUTRAL":
        reasoning.append(f"[CE] {ce_label}")
    if pe_conv != "NEUTRAL":
        reasoning.append(f"[PE] {pe_label}")

    return {
        "near_ce":              near_ce,
        "near_pe":              near_pe,
        "far_ce":               far_ce,
        "far_pe":               far_pe,
        "ce_conviction":        ce_conv,
        "ce_conviction_label":  ce_label,
        "pe_conviction":        pe_conv,
        "pe_conviction_label":  pe_label,
        "combined_signal":      comb_conv,
        "combined_label":       comb_label,
        "reasoning":            reasoning,
        "gated":                False,
    }


# ── AUTO gate helpers ─────────────────────────────────────────────────────────

def check_pe_activity_gate(pe_class):
    if not pe_class:
        return True, "PE classifier unavailable"
    t = pe_class.get("type", "UNCLEAR")
    c = pe_class.get("confidence", 0)
    if t == "FEAR" and c > 60:
        return False, f"PE activity=FEAR ({c:.0f}% confidence) — do not sell puts"
    return True, f"PE activity={t} ({c:.0f}%)"


def check_ce_activity_gate(ce_class):
    if not ce_class:
        return True, "CE classifier unavailable"
    t = ce_class.get("type", "UNCLEAR")
    c = ce_class.get("confidence", 0)
    if t == "FEAR" and c > 60:
        return False, f"CE activity=FEAR ({c:.0f}% confidence) — do not sell calls"
    return True, f"CE activity={t} ({c:.0f}%)"


# ── Historical log ────────────────────────────────────────────────────────────

def append_activity_log(pe_class, ce_class, spot, vix):
    try:
        os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
        try:
            with open(_LOG_PATH, "r") as f:
                log = json.load(f)
        except Exception:
            log = []
        log.append({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "spot": spot, "vix": vix,
            "pe":   pe_class, "ce": ce_class,
        })
        if len(log) > 5000:
            log = log[-5000:]
        with open(_LOG_PATH, "w") as f:
            json.dump(log, f)
    except Exception as e:
        print(f"[PE_CLASS] log write failed: {e}")


# ── Internal ──────────────────────────────────────────────────────────────────

def _unclear(reason):
    return {
        "type": "UNCLEAR", "confidence": 0,
        "scores": {"FEAR": 0, "WRITING": 0, "HEDGE_SPREAD": 0},
        "reasoning": [reason],
        "buckets": {"oi_has_data": False},
    }

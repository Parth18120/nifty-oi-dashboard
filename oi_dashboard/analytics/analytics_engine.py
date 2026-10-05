"""
analytics_engine.py — rolling edge statistics, P&L attribution,
regime-conditional performance, and equity curve.

Three questions professionals ask:
  1. "What is your edge?"              → rolling_edge_stats()
  2. "What drives your P&L?"          → trade_attribution()
  3. "Does your model know when NOT to trade?" → regime_conditional_perf()
"""
import math
from datetime import datetime, timedelta, timezone
from statistics import mean, stdev

IST = timezone(timedelta(hours=5, minutes=30))

try:
    from config import DIVIDEND_YIELD, LOT_SIZE, RISK_FREE_RATE
except ImportError:
    LOT_SIZE       = 65
    RISK_FREE_RATE = 0.065
    DIVIDEND_YIELD = 0.012


# ── rolling_edge_stats ────────────────────────────────────────────────────────

def rolling_edge_stats(trades: list, window: int = 20) -> dict:
    """
    Compute edge statistics from the last `window` closed trades.
    Requires status='CLOSED' trades — filter before calling.
    Returns {'insufficient_data': True} if fewer than 3 trades.
    """
    recent = trades[-window:] if len(trades) >= window else trades
    n = len(recent)
    if n < 3:
        return {"insufficient_data": True, "n": n}

    wins   = [t for t in recent if (t.get("net_pnl") or t.get("pnl_net") or 0) > 0]
    losses = [t for t in recent if (t.get("net_pnl") or t.get("pnl_net") or 0) <= 0]

    def _pnl(t):
        return float(t.get("net_pnl") or t.get("pnl_net") or 0)

    win_rate  = len(wins) / n
    avg_win   = mean(_pnl(t) for t in wins)   if wins   else 0.0
    avg_loss  = abs(mean(_pnl(t) for t in losses)) if losses else 0.0
    edge      = win_rate * avg_win - (1 - win_rate) * avg_loss

    # Kelly fraction (capped at 25%)
    b = avg_win / avg_loss if avg_loss > 0 else 0.0
    if b > 0:
        kelly = (b * win_rate - (1 - win_rate)) / b
        kelly = max(0.0, min(kelly, 0.25))
    else:
        kelly = 0.0
    half_kelly = kelly / 2.0

    # Annualised Sharpe (assume ~150 trades/year)
    pnls    = [_pnl(t) for t in recent]
    m_pnl   = mean(pnls)
    s_pnl   = stdev(pnls) if n > 1 else 0.0
    sharpe  = round(m_pnl / s_pnl * math.sqrt(150), 2) if s_pnl > 0 else 0.0

    # Streaks
    results = [1 if _pnl(t) > 0 else -1 for t in recent]
    longest_win = longest_loss = 0
    run_w = run_l = 0
    for r in results:
        if r == 1:
            run_w += 1; run_l = 0
        else:
            run_l += 1; run_w = 0
        longest_win  = max(longest_win,  run_w)
        longest_loss = max(longest_loss, run_l)
    current_streak = 0
    for r in reversed(results):
        if current_streak == 0:
            current_streak = r
        elif (current_streak > 0 and r == 1) or (current_streak < 0 and r == -1):
            current_streak += r
        else:
            break

    # Exit reason breakdown
    def _reason(t):
        return str(t.get("exit_reason") or "").upper()
    pct_stopped  = round(sum(1 for t in recent if _reason(t) == "STOP")   / n * 100, 1)
    pct_targeted = round(sum(1 for t in recent if _reason(t) == "TARGET") / n * 100, 1)
    pct_eod      = round(sum(1 for t in recent if _reason(t).startswith("EOD")) / n * 100, 1)

    confidence = "LOW" if n < 30 else ("MEDIUM" if n < 100 else "HIGH")

    return {
        "n":                   n,
        "win_rate":            round(win_rate * 100, 1),
        "avg_win":             round(avg_win, 0),
        "avg_loss":            round(avg_loss, 0),
        "edge_per_trade":      round(edge, 0),
        "kelly_pct":           round(kelly * 100, 1),
        "half_kelly_pct":      round(half_kelly * 100, 1),
        "sharpe":              sharpe,
        "longest_win_streak":  longest_win,
        "longest_loss_streak": longest_loss,
        "current_streak":      current_streak,
        "pct_stopped":         pct_stopped,
        "pct_targeted":        pct_targeted,
        "pct_eod":             pct_eod,
        "confidence":          confidence,
        "insufficient_data":   False,
    }


# ── trade_attribution ─────────────────────────────────────────────────────────

def trade_attribution(trade: dict, spot_exit: float,
                      iv_exit: float | None) -> dict:
    """
    Decompose a single closed trade's P&L into Greek contributions.
    All Rs amounts are for the SELL position (theta earns, delta/vega may hurt).
    """
    from oi_dashboard.analytics.greeks_engine import compute_greeks

    entry_spot  = float(trade.get("entry_spot") or spot_exit)
    sigma_used  = float(trade.get("sigma_used") or 13.0)
    strike      = int(trade.get("strike") or 24500)
    opt_type    = str(trade.get("opt_type") or "CE").upper()
    lots        = int(trade.get("lots") or 1)
    gross_pnl   = float(trade.get("gross_pnl") or trade.get("pnl_total") or 0)
    qty         = lots * LOT_SIZE

    # Estimate days_held from entry/exit times (same calendar day assumed)
    days_held = 0.5
    try:
        et = str(trade.get("entry_time") or "")
        xt = str(trade.get("exit_time") or "")
        if et and xt and len(et) >= 5 and len(xt) >= 5:
            fmt = "%H:%M:%S" if len(et) >= 8 else "%H:%M"
            t0 = datetime.strptime(et[:8] if len(et) >= 8 else et[:5], fmt)
            t1 = datetime.strptime(xt[:8] if len(xt) >= 8 else xt[:5], fmt)
            hours = (t1 - t0).total_seconds() / 3600.0
            days_held = max(hours / 6.25, 0.01)
    except Exception:
        pass

    # Greeks at entry — use default DTE=3 (typical weekly trade)
    dte_entry = float(trade.get("dte_at_entry") or 3.0)
    try:
        g = compute_greeks(entry_spot, strike, sigma_used, max(dte_entry, 0.5),
                           opt_type, RISK_FREE_RATE, DIVIDEND_YIELD)
    except Exception:
        g = {"delta": 0.0, "theta_day": 0.0, "vega_1pct": 0.0}

    theta_pnl  = abs(g.get("theta_day") or g.get("theta", 0)) * days_held * qty
    delta_pnl  = -(g.get("delta") or 0) * (spot_exit - entry_spot) * qty
    vega_estimated = iv_exit is not None
    if vega_estimated:
        iv_delta   = float(iv_exit) - sigma_used
        vega_pnl   = -(g.get("vega_1pct") or g.get("vega", 0)) * iv_delta * qty
    else:
        vega_pnl   = 0.0
    residual   = gross_pnl - theta_pnl - delta_pnl - vega_pnl

    gross_abs  = abs(gross_pnl) if gross_pnl else 1.0
    theta_share = round(theta_pnl / gross_abs * 100, 1)
    delta_share = round(delta_pnl / gross_abs * 100, 1)
    vega_share  = round(vega_pnl  / gross_abs * 100, 1)
    resid_share = round(residual  / gross_abs * 100, 1)

    if theta_share > 50:
        verdict = "THETA-DRIVEN"
    elif abs(delta_share) > 40:
        verdict = "DIRECTION-DRIVEN"
    elif abs(vega_share) > 30:
        verdict = "VEGA-DRIVEN"
    else:
        verdict = "MIXED"

    return {
        "theta_pnl":    round(theta_pnl,  0),
        "delta_pnl":    round(delta_pnl,  0),
        "vega_pnl":     round(vega_pnl,   0),
        "residual":     round(residual,   0),
        "theta_share":  theta_share,
        "delta_share":  delta_share,
        "vega_share":   vega_share,
        "resid_share":  resid_share,
        "days_held":    round(days_held, 3),
        "verdict":      verdict,
        "vega_estimated": vega_estimated,
    }


# ── attribute_all_closed ──────────────────────────────────────────────────────

def attribute_all_closed(date: str = None) -> list:
    """
    Run trade_attribution() on all closed trades for `date` (default today).
    Fetches nearest surface snapshot as iv_exit proxy.
    Updates attribution columns in DB.
    """
    from oi_dashboard.storage import db
    if date is None:
        date = datetime.now(IST).strftime("%Y-%m-%d")

    trades = db.get_trades(date=date)
    closed = [t for t in trades if t.get("status") != "OPEN"]

    # Load surface snapshots for IV proxy
    snaps = []
    try:
        snaps = db.get_surface_snapshots(days=1)
    except Exception:
        pass

    def _nearest_iv(exit_time_str):
        if not snaps or not exit_time_str:
            return None
        try:
            ext = exit_time_str[:8]  # HH:MM:SS
            best, best_diff = None, float("inf")
            for sn in snaps:
                sn_t = str(sn.get("ts") or "")[-8:]
                diff = abs(_hhmmss(ext) - _hhmmss(sn_t))
                if diff < best_diff:
                    best, best_diff = sn, diff
            return float(best["atm_iv"]) if best else None
        except Exception:
            return None

    def _hhmmss(s):
        parts = s.split(":")
        if len(parts) < 2:
            return 0
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2] if len(parts) > 2 else 0)

    results = []
    for t in closed:
        try:
            iv_exit   = _nearest_iv(t.get("exit_time"))
            spot_exit = float(t.get("spot_at_exit") or t.get("entry_spot") or 24500)
            attr      = trade_attribution(t, spot_exit, iv_exit)
            db.update_paper_trade_attribution(t["id"], **{
                "theta_pnl":   attr["theta_pnl"],
                "delta_pnl":   attr["delta_pnl"],
                "vega_pnl":    attr["vega_pnl"],
                "residual_pnl": attr["residual"],
                "days_held":   attr["days_held"],
                "iv_at_exit":  iv_exit,
            })
            results.append({"trade_id": t["id"], **attr})
        except Exception as e:
            print(f"[ANALYTICS] attribution failed for trade {t.get('id')}: {e}")
    return results


# ── regime_conditional_perf ───────────────────────────────────────────────────

def regime_conditional_perf(trades: list) -> list:
    """
    Split closed trades by vix_at_entry z-score into 4 regime buckets.
    Uses db.get_surface_snapshots() as VIX proxy when direct VIX history absent.
    """
    from oi_dashboard.storage import db

    closed = [t for t in trades if t.get("status") != "OPEN"]

    # Build VIX/IV proxy distribution from surface_snapshots
    iv_proxy_mean = iv_proxy_std = None
    try:
        snaps = db.get_surface_snapshots(days=90)
        ivs   = [float(s["atm_iv"]) for s in snaps if s.get("atm_iv")]
        if len(ivs) >= 5:
            iv_proxy_mean = mean(ivs)
            iv_proxy_std  = stdev(ivs) if len(ivs) > 1 else 1.0
    except Exception:
        pass

    def _bucket(t):
        vix = t.get("vix_at_entry")
        if vix is None:
            return "Unknown"
        if iv_proxy_mean and iv_proxy_std and iv_proxy_std > 0:
            z = (float(vix) - iv_proxy_mean) / iv_proxy_std
        else:
            z = 0.0
        if z < -1.0:  return "Calm"
        if z <= 1.0:  return "Normal"
        if z <= 2.0:  return "Elevated"
        return "High"

    bucket_order = ["Calm", "Normal", "Elevated", "High", "Unknown"]
    buckets = {b: [] for b in bucket_order}
    for t in closed:
        buckets[_bucket(t)].append(t)

    def _pnl(t):
        return float(t.get("net_pnl") or t.get("pnl_net") or 0)

    results = []
    for b in bucket_order:
        ts = buckets[b]
        if not ts:
            continue
        pnls   = [_pnl(t) for t in ts]
        wins   = sum(1 for p in pnls if p > 0)
        m_pnl  = mean(pnls)
        s_pnl  = stdev(pnls) if len(pnls) > 1 else 0.0
        sharpe = round(m_pnl / s_pnl * math.sqrt(150), 2) if s_pnl > 0 else 0.0
        results.append({
            "bucket":       b,
            "count":        len(ts),
            "win_rate":     round(wins / len(ts) * 100, 1),
            "avg_net_pnl":  round(m_pnl, 0),
            "sharpe":       sharpe,
        })
    return results


# ── equity_curve ──────────────────────────────────────────────────────────────

def equity_curve(trades: list) -> list:
    """
    Running cumulative net P&L and drawdown from peak.
    Input: closed trades — sort by exit_time.
    """
    def _sort_key(t):
        return str(t.get("exit_time") or t.get("entry_time") or "")

    closed = sorted(
        [t for t in trades if t.get("status") != "OPEN"],
        key=_sort_key
    )

    cum_pnl = 0.0
    peak    = 0.0
    curve   = []
    for t in closed:
        net = float(t.get("net_pnl") or t.get("pnl_net") or 0)
        cum_pnl += net
        peak     = max(peak, cum_pnl)
        dd_rs    = round(cum_pnl - peak, 0)
        dd_pct   = round(dd_rs / peak * 100, 1) if peak > 0 else 0.0
        curve.append({
            "trade_id":     t.get("id"),
            "date":         str(t.get("trade_date") or "")[:10],
            "cum_pnl":      round(cum_pnl, 0),
            "peak":         round(peak, 0),
            "drawdown_rs":  dd_rs,
            "drawdown_pct": dd_pct,
            "net_pnl":      round(net, 0),
        })
    return curve

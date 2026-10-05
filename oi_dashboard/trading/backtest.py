"""
backtest.py — synthetic backtest engine.

Simulates the current model on historical daily OHLC using BS pricing
as a proxy for real option prices. Not a perfect simulation (real option
prices deviate from BS, especially around events) but sufficient to:
  1. Validate that IV > RV edge is real on this data
  2. Compare parameter combinations (OVR threshold, stop mult, distance)
  3. Produce a tearsheet answering "does your model work?"

NOTE: VIX floor in sigma_forecast() does NOT apply here — no historical
VIX data. This is intentional and conservative: if edge shows up without
the VIX floor, it's real.

Usage:
  python backtest.py                     # run with current config
  python backtest.py --sweep             # parameter sweep
  python backtest.py --days 60           # limit to last N days
  python backtest.py --export results.xlsx
"""
import math
import os
import sys

from oi_dashboard.analytics import vol_engine as VE
from oi_dashboard.analytics.analytics_engine import equity_curve, rolling_edge_stats
from oi_dashboard.storage import db

# ── Config defaults (overridable via config_overrides) ────────────────────────
try:
    from config import (
        BT_IV_PREMIUM_PP,
        COSTS,
        FAIR_FLOOR,
        LOT_SIZE,
        MAX_DISTANCE,
        MAX_LTP,
        MIN_DISTANCE,
        MIN_LTP,
        OVR_THRESHOLD,
        STOP_MULT,
        STRUCTURAL_IV_PREMIUM_PP,
        TARGET_DECAY,
    )
except ImportError:
    LOT_SIZE                 = 65
    COSTS                    = {}
    MIN_LTP                  = 8.0
    MAX_LTP                  = 80.0
    MIN_DISTANCE             = 300
    MAX_DISTANCE             = 800
    STOP_MULT                = 2.0
    TARGET_DECAY             = 0.60
    STRUCTURAL_IV_PREMIUM_PP = 0.03
    OVR_THRESHOLD            = 40.0
    FAIR_FLOOR               = 0.05
    BT_IV_PREMIUM_PP         = 2.0

STRIKE_STEP = 50   # Nifty step size


# ── Cost calculation ──────────────────────────────────────────────────────────

_SLIPPAGE_RS = 1.0   # ₹1 slippage per share each side (conservative)


def _trade_costs(entry_ltp: float, exit_ltp: float, lots: int) -> float:
    """Full round-trip costs for one strangle leg."""
    units         = lots * LOT_SIZE
    sell_turnover = entry_ltp * units
    buy_turnover  = exit_ltp  * units
    turnover      = sell_turnover + buy_turnover

    brokerage = COSTS.get("brokerage_per_order", 20) * 2   # entry + exit
    exch_fee  = turnover      * COSTS.get("exchange_txn_pct", 0.053) / 100
    sebi_fee  = turnover      * COSTS.get("sebi_pct", 0.0001) / 100
    stt       = sell_turnover * COSTS.get("stt_sell_premium_pct", 0.0625) / 100
    stamp     = buy_turnover  * COSTS.get("stamp_duty_pct", 0.003) / 100
    gst       = (brokerage + exch_fee) * COSTS.get("gst_pct", 18.0) / 100
    slippage  = _SLIPPAGE_RS * 2 * units   # entry + exit slippage

    return round(brokerage + exch_fee + sebi_fee + stt + stamp + gst + slippage, 2)


# ── DTE helper ────────────────────────────────────────────────────────────────

def _dte_for_date(date_str: str) -> float | None:
    """
    Days to next Thursday expiry (Nifty changed from Thursday … stays Thursday).
    Returns None on expiry day (Thursday) or if DTE < 2.
    """
    from datetime import datetime
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d")
    except ValueError:
        return None

    # weekday(): Monday=0, Thursday=3
    dow = d.weekday()
    days_to_thursday = (3 - dow) % 7
    if days_to_thursday == 0:
        return None   # it's Thursday — expiry day, skip
    dte = float(days_to_thursday)
    if dte < 2:
        return None   # too close
    if dte > 7:
        dte = 5.0     # mid-week approximation for long weeks
    return dte


# ── Single-day simulation ─────────────────────────────────────────────────────

def _simulate_day(day: dict, rows_so_far: list, cfg: dict, bt_iv_premium_frac: float = 0.02) -> list:
    """
    Simulate one trading day. Returns list of 0, 1, or 2 trade dicts.
    rows_so_far: all OHLC rows up to (not including) this day — no lookahead.
    """
    min_ltp      = cfg["MIN_LTP"]
    max_ltp      = cfg["MAX_LTP"]
    min_dist     = cfg["MIN_DISTANCE"]
    max_dist     = cfg["MAX_DISTANCE"]
    stop_mult    = cfg["STOP_MULT"]
    target_decay = cfg["TARGET_DECAY"]
    ovr_thresh   = cfg.get("OVR_THRESHOLD", OVR_THRESHOLD)
    lots         = cfg["lots"]

    date   = day["date"]
    open_  = float(day.get("open")  or day.get("close") or 0)
    high   = float(day.get("high")  or open_)
    low    = float(day.get("low")   or open_)
    close  = float(day.get("close") or open_)

    if open_ < 100:
        return []

    dte = _dte_for_date(date)
    if dte is None:
        return []

    # Sigma — no VIX proxy in backtest (intentionally conservative)
    ohlc_rows  = rows_so_far
    closes_seq = [r["close"] for r in rows_so_far if r.get("close", 0) > 0]
    rvs        = VE.compute_all_rvs(ohlc_rows, closes_seq)
    sigma      = VE.sigma_forecast(rvs, vix=None)   # no VIX floor in backtest

    if sigma <= 0.08:
        sigma = 0.08   # hard min already in sigma_forecast, but be explicit

    atm = round(open_ / STRIKE_STEP) * STRIKE_STEP
    trades = []

    for side in ("CE", "PE"):
        best_strike = best_entry = best_fair = best_ovr = best_dist_sd = None

        if side == "CE":
            lo = atm + min_dist
            hi = atm + max_dist
        else:
            lo = atm - max_dist
            hi = atm - min_dist

        rng = range(int(lo), int(hi) + 1, STRIKE_STEP)
        if side == "PE":
            rng = range(int(hi), int(lo) - 1, -STRIKE_STEP)

        for strike in rng:
            # Entry at MARKET price (realized vol + synthetic IV premium)
            # This simulates real market conditions where IV > RV.
            market_sigma = sigma + bt_iv_premium_frac
            entry_ltp = VE.bs_price(open_, strike, market_sigma, dte, side)
            if not (min_ltp <= entry_ltp <= max_ltp):
                continue

            # Distance in standard deviations (sigma is a fraction e.g. 0.108)
            dist_pts = abs(strike - open_)
            sd_move  = open_ * sigma * math.sqrt(dte / 365.0)
            dist_sd  = dist_pts / sd_move if sd_move > 0 else 0.0

            if not (1.0 <= dist_sd <= 2.5):
                continue

            # OVR%: how much does market price exceed BS fair value (at sigma)?
            # Meaningful now because market_sigma != sigma.
            fair = VE.bs_price(open_, strike, sigma, dte, side)
            if fair >= FAIR_FLOOR:
                ovr_pct = (entry_ltp - fair) / fair * 100.0
            else:
                ovr_pct = 0.0

            # Rank by OVR% (prefer most overpriced within distance band)
            if best_ovr is None or ovr_pct > best_ovr:
                best_ovr     = ovr_pct
                best_dist_sd = dist_sd
                best_strike  = strike
                best_entry   = entry_ltp

        if best_strike is None:
            continue

        # Simulate exit
        stop_price   = best_entry * stop_mult
        target_price = best_entry * (1 - target_decay)

        if side == "CE":
            adverse_spot = high
            iv_spike     = sigma * 1.15   # IV spikes on up-moves
        else:
            adverse_spot = low
            iv_spike     = sigma * 1.25   # IV spikes harder on down-moves

        worst_ltp = VE.bs_price(adverse_spot, best_strike, iv_spike,
                                max(dte - 0.3, 0.02), side)
        eod_ltp   = VE.bs_price(close, best_strike, sigma,
                                max(dte - 1.0, 0.02), side)

        if worst_ltp >= stop_price:
            exit_ltp    = stop_price
            exit_reason = "STOP"
        elif eod_ltp <= target_price:
            exit_ltp    = eod_ltp
            exit_reason = "TARGET"
        else:
            exit_ltp    = eod_ltp
            exit_reason = "EOD"

        gross_pnl = (best_entry - exit_ltp) * lots * LOT_SIZE
        costs_rs  = _trade_costs(best_entry, exit_ltp, lots)
        net_pnl   = gross_pnl - costs_rs

        dist_pts  = best_strike - open_ if side == "CE" else open_ - best_strike

        trades.append({
            "date":        date,
            "opt_type":    side,
            "strike":      int(best_strike),
            "distance_pts": round(dist_pts, 0),
            "distance_sd": round(best_dist_sd, 2),
            "entry":       round(best_entry, 2),
            "exit":        round(exit_ltp, 2),
            "exit_reason": exit_reason,
            "sigma":       round(sigma * 100, 2),       # realized vol %
            "market_sigma": round(market_sigma * 100, 2), # entry IV %
            "ovr_pct":     round(best_ovr, 1),            # market vs fair
            "dte":         dte,
            "gross_pnl":   round(gross_pnl, 0),
            "costs":       round(costs_rs, 0),
            "net_pnl":     round(net_pnl, 0),
            # for analytics_engine compatibility
            "pnl_net":     round(net_pnl, 0),
            "pnl_total":   round(gross_pnl, 0),
            "status":      "CLOSED",
            "lots":        lots,
            "day_open":    round(open_, 1),
            "day_high":    round(high, 1),
            "day_low":     round(low, 1),
            "day_close":   round(close, 1),
        })

    return trades


# ── run_backtest ──────────────────────────────────────────────────────────────

def run_backtest(spot_rows: list, config_overrides: dict = None) -> dict:
    """
    Simulate one strangle per day on historical OHLC data.
    spot_rows: sorted list of {date, open, high, low, close} dicts.
    """
    cfg = {
        "MIN_LTP":          MIN_LTP,
        "MAX_LTP":          MAX_LTP,
        "MIN_DISTANCE":     MIN_DISTANCE,
        "MAX_DISTANCE":     MAX_DISTANCE,
        "STOP_MULT":        STOP_MULT,
        "TARGET_DECAY":     TARGET_DECAY,
        "OVR_THRESHOLD":    OVR_THRESHOLD,
        "BT_IV_PREMIUM_PP": BT_IV_PREMIUM_PP,
        "lots":             1,   # always 1 in backtest — Sharpe is lot-independent
    }
    if config_overrides:
        cfg.update(config_overrides)

    bt_iv_premium_frac = cfg["BT_IV_PREMIUM_PP"] / 100.0

    WARMUP = 21
    all_trades   = []
    trade_dates  = set()

    for i in range(WARMUP, len(spot_rows)):
        rows_so_far = spot_rows[:i]
        day         = spot_rows[i]
        day_trades  = _simulate_day(day, rows_so_far, cfg, bt_iv_premium_frac)
        if day_trades:
            all_trades.extend(day_trades)
            trade_dates.add(day["date"])

    days_tested = len(spot_rows) - WARMUP
    days_with   = len(trade_dates)
    coverage    = round(days_with / days_tested * 100, 1) if days_tested > 0 else 0.0

    ce_trades = [t for t in all_trades if t["opt_type"] == "CE"]
    pe_trades = [t for t in all_trades if t["opt_type"] == "PE"]

    # Exit breakdown
    by_exit = {}
    for reason in ("STOP", "TARGET", "EOD"):
        grp = [t for t in all_trades if t["exit_reason"] == reason]
        if grp:
            by_exit[reason] = {
                "count":   len(grp),
                "avg_pnl": round(sum(t["net_pnl"] for t in grp) / len(grp), 0),
                "pct":     round(len(grp) / len(all_trades) * 100, 1) if all_trades else 0,
            }

    stats    = rolling_edge_stats(all_trades, window=len(all_trades))
    eq_curve = equity_curve(all_trades)

    return {
        "trades":          all_trades,
        "stats":           stats,
        "equity_curve":    eq_curve,
        "regime_perf":     [],   # no VIX in backtest
        "by_side": {
            "CE": rolling_edge_stats(ce_trades, window=len(ce_trades)),
            "PE": rolling_edge_stats(pe_trades, window=len(pe_trades)),
        },
        "by_exit_reason":  by_exit,
        "parameter_set":   {k: v for k, v in cfg.items()},
        "days_tested":     days_tested,
        "days_with_trades": days_with,
        "coverage_pct":    coverage,
    }


# ── parameter_sweep ───────────────────────────────────────────────────────────

_DEFAULT_GRID = {
    "STOP_MULT":        [1.25, 1.5, 1.75],
    "MIN_LTP":          [12, 15, 20],
    "TARGET_DECAY":     [0.40, 0.50, 0.60],
    "BT_IV_PREMIUM_PP": [BT_IV_PREMIUM_PP],   # fixed at config value during sweep
}


def parameter_sweep(spot_rows: list, param_grid: dict = None) -> list:
    """
    Test all combinations in param_grid on train (70%) / test (30%) split.
    Returns list sorted by out-of-sample Sharpe, best first.
    """
    import itertools
    grid = param_grid or _DEFAULT_GRID

    split = int(len(spot_rows) * 0.70)
    train = spot_rows[:split]
    test  = spot_rows[split:]

    keys   = list(grid.keys())
    combos = list(itertools.product(*[grid[k] for k in keys]))
    results = []

    for combo in combos:
        overrides = dict(zip(keys, combo))
        try:
            is_res  = run_backtest(train, overrides)
            oos_res = run_backtest(test,  overrides)
        except Exception as e:
            print(f"[SWEEP] Combo {overrides} failed: {e}")
            continue

        is_s  = is_res["stats"]
        oos_s = oos_res["stats"]
        is_sharpe  = is_s.get("sharpe",  0) if not is_s.get("insufficient_data")  else 0
        oos_sharpe = oos_s.get("sharpe", 0) if not oos_s.get("insufficient_data") else 0
        is_pnl  = sum(t["net_pnl"] for t in is_res["trades"])
        oos_pnl = sum(t["net_pnl"] for t in oos_res["trades"])
        overfit_ratio = (is_sharpe / oos_sharpe) if oos_sharpe > 0.01 else 99.0

        results.append({
            "params":            overrides,
            "in_sample_sharpe":  round(is_sharpe,  2),
            "out_sample_sharpe": round(oos_sharpe, 2),
            "in_sample_trades":  len(is_res["trades"]),
            "out_sample_trades": len(oos_res["trades"]),
            "in_sample_net_pnl":  round(is_pnl,  0),
            "out_sample_net_pnl": round(oos_pnl, 0),
            "overfit_ratio":     round(overfit_ratio, 1),
            "is_overfit":        overfit_ratio > 2.0,
        })

    results.sort(key=lambda r: r["out_sample_sharpe"], reverse=True)
    return results


# ── Excel export ──────────────────────────────────────────────────────────────

def _export_backtest_excel(result: dict, path: str) -> None:
    """Two-sheet Excel: Summary + Trade Log (color-coded)."""
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError:
        print("[EXPORT] openpyxl not installed — pip install openpyxl")
        return

    WIN_FILL  = PatternFill("solid", fgColor="CCFFCC")
    LOSS_FILL = PatternFill("solid", fgColor="FFCCCC")
    STOP_FILL = PatternFill("solid", fgColor="FFE0CC")
    HEAD_FONT = Font(bold=True)

    wb = openpyxl.Workbook()

    # Sheet 1: Summary
    ws1 = wb.active
    ws1.title = "Summary"
    stats  = result.get("stats", {})
    params = result.get("parameter_set", {})

    summary_rows = [
        ["BACKTEST SUMMARY", ""],
        ["Days tested",       result.get("days_tested", "")],
        ["Days with trades",  result.get("days_with_trades", "")],
        ["Total trades",      len(result.get("trades", []))],
        ["Coverage %",        result.get("coverage_pct", "")],
        ["", ""],
        ["Win rate %",        stats.get("win_rate", "")],
        ["Avg win (Rs)",      stats.get("avg_win", "")],
        ["Avg loss (Rs)",     stats.get("avg_loss", "")],
        ["Edge/trade (Rs)",   stats.get("edge_per_trade", "")],
        ["Kelly %",           stats.get("kelly_pct", "")],
        ["Half-Kelly %",      stats.get("half_kelly_pct", "")],
        ["Sharpe",            stats.get("sharpe", "")],
        ["Total net P&L",     sum(t["net_pnl"] for t in result.get("trades", []))],
        ["", ""],
        ["Parameters", ""],
    ]
    for k, v in params.items():
        summary_rows.append([k, v])

    for row in summary_rows:
        ws1.append(row)
    ws1["A1"].font = HEAD_FONT

    # Sheet 2: Trade log
    ws2 = wb.create_sheet("Trade Log")
    headers = ["Date","Side","Strike","Dist Pts","Dist SD","Entry","Exit",
               "Exit Reason","Sigma %","OVR %","DTE","Gross P&L","Costs","Net P&L"]
    ws2.append(headers)
    for cell in ws2[1]:
        cell.font = HEAD_FONT

    for t in result.get("trades", []):
        row = [
            t.get("date"), t.get("opt_type"), t.get("strike"),
            t.get("distance_pts"), t.get("distance_sd"),
            t.get("entry"), t.get("exit"), t.get("exit_reason"),
            t.get("sigma"), t.get("ovr_pct"), t.get("dte"),
            t.get("gross_pnl"), t.get("costs"), t.get("net_pnl"),
        ]
        ws2.append(row)
        last_row = ws2.max_row
        reason   = t.get("exit_reason", "")
        fill     = STOP_FILL if reason == "STOP" else (WIN_FILL if (t.get("net_pnl") or 0) > 0 else LOSS_FILL)
        for cell in ws2[last_row]:
            cell.fill = fill

    wb.save(path)
    print(f"[EXPORT] Saved to {path}")


# ── Simple in-process cache for API ──────────────────────────────────────────
import time as _time_mod

_bt_cache = {}

def get_cached_backtest(days: int, lots: int = 1) -> dict | None:
    key = (days, lots)
    entry = _bt_cache.get(key)
    if entry and (_time_mod.time() - entry["ts"]) < 300:
        return entry["result"]
    return None

def set_cached_backtest(days: int, result: dict, lots: int = 1):
    _bt_cache[(days, lots)] = {"result": result, "ts": _time_mod.time()}


# ── Real backtest (uses option_eod_history) ───────────────────────────────────

def _f(v) -> float:
    try:
        return float(v or 0)
    except (ValueError, TypeError):
        return 0.0


def _find_best_real_strike(date_str, chosen_expiry, spot_open, sigma, dte,
                           side, cfg, eod_lookup):
    """
    Find the best strike to sell using real option OPEN prices.

    Selection criteria (in order):
      1. Strike is OTM by MIN_DISTANCE..MAX_DISTANCE points from spot
      2. Distance in SD units is 1.0..2.5
      3. Option OPEN price is between MIN_LTP and MAX_LTP
      4. Among passing strikes, pick highest OPEN (most premium collected)

    Returns dict with eod row fields + chosen_dte, or None.
    """
    min_dist     = cfg["MIN_DISTANCE"]
    max_dist     = cfg["MAX_DISTANCE"]
    min_ltp      = cfg["MIN_LTP"]
    max_ltp      = cfg["MAX_LTP"]
    atm          = round(spot_open / STRIKE_STEP) * STRIKE_STEP

    if side == "CE":
        lo, hi = atm + min_dist, atm + max_dist
        rng    = range(int(lo), int(hi) + 1, STRIKE_STEP)
    else:
        lo, hi = atm - max_dist, atm - min_dist
        rng    = range(int(hi), int(lo) - 1, -STRIKE_STEP)

    sd_move     = spot_open * sigma * math.sqrt(dte / 365.0)
    best        = None
    best_open   = -1.0

    for strike in rng:
        key = (date_str, chosen_expiry, float(strike), side)
        eod = eod_lookup.get(key)
        if eod is None:
            continue

        opt_open  = _f(eod.get("open"))
        opt_high  = _f(eod.get("high"))
        opt_low   = _f(eod.get("low"))
        opt_close = _f(eod.get("close") or eod.get("settle"))

        # Entry is the option OPEN price — must be within LTP band
        if opt_open <= 0:
            continue   # illiquid at open — skip
        if not (min_ltp <= opt_open <= max_ltp):
            continue

        dist_pts = abs(strike - spot_open)
        dist_sd  = dist_pts / sd_move if sd_move > 0 else 0.0
        if not (1.0 <= dist_sd <= 2.5):
            continue

        # Rank by highest open (most premium within the distance band)
        if opt_open > best_open:
            best_open = opt_open
            best      = {
                "strike":    strike,
                "opt_open":  opt_open,
                "opt_high":  opt_high,
                "opt_low":   opt_low,
                "opt_close": opt_close,
                "dist_pts":  dist_pts,
                "dist_sd":   round(dist_sd, 2),
            }

    return best


def run_real_backtest(from_date: str = None, to_date: str = None,
                      config_overrides: dict = None) -> dict:
    """
    Backtest using real NSE option EOD prices (option_eod_history table).

    Entry  : option OPEN price on the trade date (~9:30 AM sell at open)
    Stop   : fires if option HIGH >= entry * STOP_MULT  (same-day, both CE and PE)
             exit_ltp = entry * STOP_MULT
    Target : fires if option LOW  <= entry * (1 - TARGET_DECAY)  (same-day)
             exit_ltp = entry * (1 - TARGET_DECAY)
    EOD    : neither stop nor target hit; exit_ltp = option CLOSE
    Stop is checked before target (conservative).

    Strike selection: highest option OPEN within distance/SD/LTP filters
    (most premium collected, not deepest OTM).

    open_vs_sigma: how much the real option OPEN exceeded BS fair value,
    i.e. the quantified edge that existed at entry.
    """
    from datetime import datetime as _dt

    from oi_dashboard.storage import db as _db_real

    cfg = {
        "MIN_LTP":       MIN_LTP,
        "MAX_LTP":       MAX_LTP,
        "MIN_DISTANCE":  MIN_DISTANCE,
        "MAX_DISTANCE":  MAX_DISTANCE,
        "STOP_MULT":     STOP_MULT,
        "TARGET_DECAY":  TARGET_DECAY,
        "OVR_THRESHOLD": OVR_THRESHOLD,
        "lots":          1,
    }
    if config_overrides:
        cfg.update(config_overrides)

    # Full spot history for warmup; date filter applies to *trading*, not loading
    all_spot_rows = _db_real.get_spot_daily(days=500)
    if len(all_spot_rows) < 22:
        return {"error": f"Insufficient spot data: {len(all_spot_rows)} days (need 22+). "
                         "Run the server first to accumulate spot_daily data."}

    eod_rows = _db_real.get_option_eod_history(underlying="NIFTY")
    if not eod_rows:
        return {"error": "option_eod_history is empty. "
                         "Run: python fetch_nse_bhavcopy.py --days 365"}

    # Lookup: (date, expiry, strike_float, opt_type) -> eod row
    eod_lookup = {}
    for r in eod_rows:
        eod_lookup[(r["date"], r["expiry"], float(r["strike"]), r["opt_type"])] = r

    # Available expiries per date: date -> sorted list of expiry strings
    from collections import defaultdict
    expiries_by_date = defaultdict(list)
    for r in eod_rows:
        expiries_by_date[r["date"]].append(r["expiry"])
    # Deduplicate and sort
    for d in expiries_by_date:
        expiries_by_date[d] = sorted(set(expiries_by_date[d]))

    eod_dates = set(expiries_by_date.keys())

    WARMUP      = 21
    all_trades  = []
    trade_dates = set()
    missing_eod = 0
    eod_days_in_range = 0

    for i in range(WARMUP, len(all_spot_rows)):
        rows_so_far = all_spot_rows[:i]
        day         = all_spot_rows[i]
        date_str    = day["date"]

        if from_date and date_str < from_date:
            continue
        if to_date and date_str > to_date:
            continue
        if date_str not in eod_dates:
            continue

        eod_days_in_range += 1

        spot_open  = _f(day.get("open")  or day.get("close"))
        spot_close = _f(day.get("close") or day.get("open"))
        if spot_open < 100:
            continue

        # Compute sigma identical to synthetic backtest (no VIX floor)
        closes_seq = [r["close"] for r in rows_so_far if r.get("close", 0) > 0]
        rvs        = VE.compute_all_rvs(rows_so_far, closes_seq)
        sigma      = VE.sigma_forecast(rvs, vix=None)
        sigma      = max(sigma, 0.08)

        # Pick nearest expiry with 1-14 DTE from actual EOD data
        day_dt        = _dt.strptime(date_str, "%Y-%m-%d").date()
        chosen_expiry = None
        chosen_dte    = None
        for exp_str in expiries_by_date[date_str]:
            try:
                exp_dt  = _dt.strptime(exp_str, "%Y-%m-%d").date()
                exp_dte = (exp_dt - day_dt).days
                if 1 <= exp_dte <= 14:
                    chosen_expiry = exp_str
                    chosen_dte    = float(exp_dte)
                    break
            except ValueError:
                continue

        if not chosen_expiry:
            continue

        for side in ("CE", "PE"):
            best = _find_best_real_strike(
                date_str, chosen_expiry, spot_open, sigma,
                chosen_dte, side, cfg, eod_lookup
            )
            if best is None:
                missing_eod += 1
                continue

            entry_ltp    = best["opt_open"]
            opt_high     = best["opt_high"]
            opt_low      = best["opt_low"]
            opt_close    = best["opt_close"]
            strike       = best["strike"]

            stop_price   = entry_ltp * cfg["STOP_MULT"]
            target_price = entry_ltp * (1.0 - cfg["TARGET_DECAY"])

            # Use actual option HIGH/LOW for same-day stop and target detection.
            # For a short position, loss = option price goes UP (HIGH hit).
            # Profit  = option price goes DOWN (LOW hit).
            # Stop is checked before target (conservative ordering).
            if opt_high >= stop_price:
                exit_ltp    = stop_price
                exit_reason = "STOP"
            elif opt_low <= target_price:
                exit_ltp    = target_price
                exit_reason = "TARGET"
            else:
                exit_ltp    = opt_close if opt_close > 0 else entry_ltp
                exit_reason = "EOD_SAME_DAY"

            gross_pnl = (entry_ltp - exit_ltp) * cfg["lots"] * LOT_SIZE
            costs_rs  = _trade_costs(entry_ltp, exit_ltp, cfg["lots"])
            net_pnl   = gross_pnl - costs_rs
            dist_pts  = strike - spot_open if side == "CE" else spot_open - strike

            # open_vs_sigma: real option open vs BS fair at sigma (no IV premium added)
            bs_fair = VE.bs_price(spot_open, strike, sigma, chosen_dte, side)
            if bs_fair >= FAIR_FLOOR:
                open_vs_sigma = round((entry_ltp / bs_fair - 1.0) * 100.0, 1)
            else:
                open_vs_sigma = None

            all_trades.append({
                "date":           date_str,
                "opt_type":       side,
                "strike":         int(strike),
                "expiry":         chosen_expiry,
                "distance_pts":   round(dist_pts, 0),
                "distance_sd":    best["dist_sd"],
                "entry":          round(entry_ltp, 2),
                "exit":           round(exit_ltp, 2),
                "exit_reason":    exit_reason,
                "sigma":          round(sigma * 100, 2),
                "dte":            chosen_dte,
                "gross_pnl":      round(gross_pnl, 0),
                "costs":          round(costs_rs, 0),
                "net_pnl":        round(net_pnl, 0),
                "pnl_net":        round(net_pnl, 0),
                "pnl_total":      round(gross_pnl, 0),
                "status":         "CLOSED",
                "lots":           cfg["lots"],
                "open_vs_sigma":  open_vs_sigma,
                "bs_fair":        round(bs_fair, 2) if bs_fair >= FAIR_FLOOR else None,
                "price_source":   "real_nse_eod",
                "opt_high":       round(opt_high, 2),
                "opt_low":        round(opt_low, 2),
                "opt_close":      round(opt_close, 2),
            })
            trade_dates.add(date_str)

    if len(all_trades) < 3:
        return {
            "error": f"Only {len(all_trades)} real trades found in date range "
                     f"({eod_days_in_range} EOD days checked, {missing_eod} strike misses). "
                     "EOD data may not overlap with spot history. "
                     "Try: python fetch_nse_bhavcopy.py --days 90",
            "trades": all_trades,
        }

    days_tested = eod_days_in_range
    days_with   = len(trade_dates)
    coverage    = round(days_with / days_tested * 100, 1) if days_tested > 0 else 0.0

    ce_trades = [t for t in all_trades if t["opt_type"] == "CE"]
    pe_trades = [t for t in all_trades if t["opt_type"] == "PE"]

    by_exit = {}
    for reason in ("STOP", "TARGET", "EOD_SAME_DAY"):
        grp = [t for t in all_trades if t["exit_reason"] == reason]
        if grp:
            by_exit[reason] = {
                "count":   len(grp),
                "avg_pnl": round(sum(t["net_pnl"] for t in grp) / len(grp), 0),
                "pct":     round(len(grp) / len(all_trades) * 100, 1),
            }

    # open_vs_sigma statistics
    ovs_vals = [t["open_vs_sigma"] for t in all_trades if t["open_vs_sigma"] is not None]
    avg_ovs  = round(sum(ovs_vals) / len(ovs_vals), 1) if ovs_vals else None
    ce_ovs   = [t["open_vs_sigma"] for t in ce_trades if t["open_vs_sigma"] is not None]
    pe_ovs   = [t["open_vs_sigma"] for t in pe_trades if t["open_vs_sigma"] is not None]
    avg_entry = round(sum(t["entry"] for t in all_trades) / len(all_trades), 2)

    stats    = rolling_edge_stats(all_trades, window=len(all_trades))
    eq_curve = equity_curve(all_trades)

    # Per-side stats with open_vs_sigma
    def _side_stats(trades, ovs_list):
        s = rolling_edge_stats(trades, window=len(trades))
        s["avg_open_vs_sigma"] = round(sum(ovs_list) / len(ovs_list), 1) if ovs_list else None
        return s

    return {
        "trades":              all_trades,
        "stats":               stats,
        "equity_curve":        eq_curve,
        "by_side": {
            "CE": _side_stats(ce_trades, ce_ovs),
            "PE": _side_stats(pe_trades, pe_ovs),
        },
        "by_exit_reason":      by_exit,
        "days_tested":         days_tested,
        "days_with_trades":    days_with,
        "coverage_pct":        coverage,
        "missing_eod":         missing_eod,
        "avg_open_vs_sigma":   avg_ovs,
        "avg_entry_premium":   avg_entry,
        "price_source":        "real_nse_eod",
        "note": ("Entry at option OPEN price. Stop/target detected via option HIGH/LOW same day. "
                 "EOD exit at option CLOSE. Same-day simulation only."),
    }


# ── CLI helpers ───────────────────────────────────────────────────────────────

def _print_real_result(result, label="REAL BACKTEST"):
    trades = result.get("trades", [])
    stats  = result.get("stats", {})
    print(f"\n{'='*60}")
    print(f"{label}  ({result.get('days_tested', 0)} EOD days, {len(trades)} trades)")
    print(f"Coverage: {result.get('coverage_pct', 0):.0f}% of days had trades  "
          f"| missing EOD: {result.get('missing_eod', 0)}")
    if stats.get("insufficient_data"):
        print(f"Insufficient trades ({stats.get('n', 0)})")
        return
    total_pnl = sum(t["net_pnl"] for t in trades)
    avg_ovs   = result.get("avg_open_vs_sigma")
    avg_entry = result.get("avg_entry_premium", 0)
    print(f"  Win rate:          {stats['win_rate']:.1f}%")
    print(f"  Avg win:          Rs{stats['avg_win']:,.0f}")
    print(f"  Avg loss:         Rs{stats['avg_loss']:,.0f}")
    print(f"  Edge/trade:       Rs{stats['edge_per_trade']:,.0f}")
    print(f"  Sharpe:            {stats['sharpe']:.2f}")
    print(f"  Total net P&L:    Rs{total_pnl:,.0f}")
    print(f"  Avg entry premium: {avg_entry:.1f}")
    if avg_ovs is not None:
        print(f"  Avg open_vs_sigma: {avg_ovs:+.1f}%")
    print(f"\n  By side:")
    for side in ("CE", "PE"):
        s   = result.get("by_side", {}).get(side, {})
        ovs = s.get("avg_open_vs_sigma")
        if not s.get("insufficient_data"):
            ovs_str = f"  ovs {ovs:+.1f}%" if ovs is not None else ""
            print(f"    {side}: {s['win_rate']:.0f}% wins  Sharpe {s['sharpe']:.2f}  "
                  f"edge Rs{s['edge_per_trade']:,.0f}/trade  ({s['n']} trades){ovs_str}")
    print(f"\n  Exit breakdown:")
    for reason, data in result.get("by_exit_reason", {}).items():
        print(f"    {reason:14s}: {data['count']:3d} trades ({data['pct']:.0f}%)  "
              f"avg Rs{data['avg_pnl']:,.0f}")


def _print_synthetic_result(result, label="SYNTHETIC BACKTEST"):
    trades = result.get("trades", [])
    stats  = result.get("stats", {})
    print(f"\n{'='*60}")
    print(f"{label}  ({result.get('days_tested', 0)} days, {len(trades)} trades)")
    print(f"Coverage: {result.get('coverage_pct', 0):.0f}% of days had trades")
    if stats.get("insufficient_data"):
        print(f"Insufficient trades ({stats.get('n', 0)})")
        return
    total_pnl = sum(t["net_pnl"] for t in trades)
    print(f"  Win rate:          {stats['win_rate']:.1f}%")
    print(f"  Avg win:          Rs{stats['avg_win']:,.0f}")
    print(f"  Avg loss:         Rs{stats['avg_loss']:,.0f}")
    print(f"  Edge/trade:       Rs{stats['edge_per_trade']:,.0f}")
    print(f"  Kelly:             {stats['kelly_pct']:.1f}%")
    print(f"  Half-Kelly:        {stats['half_kelly_pct']:.1f}%")
    print(f"  Sharpe:            {stats['sharpe']:.2f}")
    print(f"  Total net P&L:    Rs{total_pnl:,.0f}")
    print(f"\n  By side:")
    for side in ("CE", "PE"):
        s = result["by_side"].get(side, {})
        if not s.get("insufficient_data"):
            print(f"    {side}: {s['win_rate']:.0f}% wins  Sharpe {s['sharpe']:.2f}  "
                  f"edge Rs{s['edge_per_trade']:,.0f}/trade  ({s['n']} trades)")
    print(f"\n  Exit breakdown:")
    for reason, data in result.get("by_exit_reason", {}).items():
        print(f"    {reason:8s}: {data['count']:3d} trades ({data['pct']:.0f}%)  "
              f"avg Rs{data['avg_pnl']:,.0f}")


def _print_comparison(real_r, synth_r):
    r_stats = real_r.get("stats", {})
    s_stats = synth_r.get("stats", {})
    if r_stats.get("insufficient_data") or s_stats.get("insufficient_data"):
        print("\nInsufficient data for comparison.")
        return
    r_sharpe  = r_stats.get("sharpe", 0)
    s_sharpe  = s_stats.get("sharpe", 0)
    r_wr      = r_stats.get("win_rate", 0)
    s_wr      = s_stats.get("win_rate", 0)
    r_edge    = r_stats.get("edge_per_trade", 0)
    s_edge    = s_stats.get("edge_per_trade", 0)
    r_pnl     = sum(t["net_pnl"] for t in real_r.get("trades", []))
    s_pnl     = sum(t["net_pnl"] for t in synth_r.get("trades", []))
    avg_ovs   = real_r.get("avg_open_vs_sigma")
    avg_entry = real_r.get("avg_entry_premium", 0)
    print(f"\n{'='*60}")
    print(f"COMPARISON  (same date window)")
    print(f"{'Metric':<24} {'SYNTHETIC':>12} {'REAL':>12} {'Delta':>10}")
    print("-" * 60)
    print(f"{'Sharpe':<24} {s_sharpe:>12.2f} {r_sharpe:>12.2f} {r_sharpe-s_sharpe:>+10.2f}")
    print(f"{'Win rate %':<24} {s_wr:>12.1f} {r_wr:>12.1f} {r_wr-s_wr:>+10.1f}")
    print(f"{'Edge/trade Rs':<24} {s_edge:>12,.0f} {r_edge:>12,.0f} {r_edge-s_edge:>+10,.0f}")
    print(f"{'Total net P&L Rs':<24} {s_pnl:>12,.0f} {r_pnl:>12,.0f} {r_pnl-s_pnl:>+10,.0f}")
    print(f"{'Trades':<24} {len(synth_r.get('trades',[])):>12} {len(real_r.get('trades',[])):>12}")
    if avg_ovs is not None:
        print(f"{'Avg open_vs_sigma':<24} {'N/A':>12} {avg_ovs:>+11.1f}%")
    if avg_entry > 0:
        print(f"{'Avg entry premium':<24} {'N/A':>12} {avg_entry:>12.1f}")
    print("-" * 60)
    print(f"\nINTERPRETATION:")
    sharpe_gap = r_sharpe - s_sharpe
    if sharpe_gap < -0.5:
        print(f"  Synthetic Sharpe ({s_sharpe:.2f}) overstates real ({r_sharpe:.2f}). "
              f"Gap {abs(sharpe_gap):.2f} = BS assumptions inflate paper performance.")
    elif sharpe_gap > 0.3:
        print(f"  Real Sharpe ({r_sharpe:.2f}) beats synthetic ({s_sharpe:.2f}) by {sharpe_gap:.2f}. "
              f"Real opens priced above BS fair — pricing inefficiency confirmed.")
    else:
        print(f"  Real and synthetic Sharpe within {abs(sharpe_gap):.2f} — broadly consistent.")
    if avg_ovs is not None:
        if avg_ovs > 5:
            print(f"  avg open_vs_sigma {avg_ovs:+.1f}%: NIFTY option opens are significantly "
                  f"richer than BS model. Market prices ~{avg_ovs:.0f}% IV premium at open.")
        elif avg_ovs > 1:
            print(f"  avg open_vs_sigma {avg_ovs:+.1f}%: mild premium over BS fair at open.")
        elif avg_ovs < -1:
            print(f"  avg open_vs_sigma {avg_ovs:+.1f}%: option opens CHEAPER than BS fair. "
                  f"Synthetic edge may not exist in real prices.")
        else:
            print(f"  avg open_vs_sigma {avg_ovs:+.1f}%: real opens close to BS fair.")
    if r_wr < 50:
        print(f"  Real win rate {r_wr:.0f}% < 50%: stops firing too often relative to profits.")
    if r_edge > 0:
        print(f"  Positive real edge Rs{r_edge:,.0f}/trade across {len(real_r.get('trades',[]))} trades.")
    else:
        print(f"  Negative real edge — strategy NOT profitable in this window on real prices.")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Nifty options backtest engine")
    parser.add_argument("--days",    type=int, default=90,  help="Days of history to use")
    parser.add_argument("--sweep",   action="store_true",   help="Run parameter sweep")
    parser.add_argument("--export",  type=str, default=None, help="Export to Excel path")
    parser.add_argument("--lots",    type=int, default=1,   help="Lot size for P&L scaling")
    parser.add_argument("--real",    action="store_true",   help="Use real NSE EOD option prices")
    parser.add_argument("--compare", action="store_true",   help="Run both real and synthetic and compare")
    parser.add_argument("--from",    dest="from_date", default=None, help="Start date YYYY-MM-DD")
    parser.add_argument("--to",      dest="to_date",   default=None, help="End date YYYY-MM-DD")
    args = parser.parse_args()

    from datetime import date as _date_
    from datetime import timedelta as _td
    from_d = args.from_date
    to_d   = args.to_date
    if not from_d and (args.real or args.compare) and args.days:
        from_d = (_date_.today() - _td(days=args.days)).isoformat()

    # ── --compare: run both real and synthetic ─────────────────────────────────
    if args.compare:
        print(f"COMPARE MODE  ({from_d or 'all'} to {to_d or 'latest'})")
        print("Running real backtest ...")
        real_r = run_real_backtest(from_d, to_d, {"lots": args.lots})
        if "error" in real_r and "trades" not in real_r:
            print(f"ERROR (real): {real_r['error']}")
            sys.exit(1)
        print("Running synthetic backtest on same date window ...")
        all_rows = db.get_spot_daily(500)
        warmup   = [r for r in all_rows if not from_d or r["date"] < from_d]
        window   = [r for r in all_rows if (not from_d or r["date"] >= from_d)
                    and (not to_d or r["date"] <= to_d)]
        synth_r  = run_backtest(warmup + window, {"lots": args.lots})
        _print_real_result(real_r, label="REAL BACKTEST (NSE EOD open/close)")
        _print_synthetic_result(synth_r, label="SYNTHETIC BACKTEST (BS pricing)")
        _print_comparison(real_r, synth_r)
        print(f"\nNote (real): {real_r.get('note','')}")
        if args.export:
            _export_backtest_excel(real_r, args.export)
            print(f"Exported real to {args.export}")
        print("\nDone.")
        sys.exit(0)

    # ── --real only ────────────────────────────────────────────────────────────
    if args.real:
        print(f"REAL BACKTEST  ({from_d or 'all'} to {to_d or 'latest'})")
        result = run_real_backtest(from_d, to_d, {"lots": args.lots})
        if "error" in result and "trades" not in result:
            print(f"ERROR: {result['error']}")
            sys.exit(1)
        _print_real_result(result)
        print(f"\nNote: {result.get('note','')}")
        if args.export:
            _export_backtest_excel(result, args.export)
        print("\nDone.")
        sys.exit(0)

    # ── synthetic (default) ────────────────────────────────────────────────────
    rows = db.get_spot_daily(args.days + 25)   # extra rows for warmup
    if len(rows) < 22:
        print(f"Only {len(rows)} spot rows — need at least 22. Run server first to load data.")
        sys.exit(1)

    bt_prem = getattr(__import__('config'), 'BT_IV_PREMIUM_PP', 2.0)
    print(f"Loaded {len(rows)} days: {rows[0]['date']} to {rows[-1]['date']}")
    print(f"IMPORTANT: Entry prices use realized vol + {bt_prem:.1f}pp synthetic IV premium")
    print(f"(BT_IV_PREMIUM_PP={bt_prem} in config). This simulates real market conditions")
    print(f"where IV > RV. The edge measured here is mean-reversion of IV toward RV")
    print(f"over the holding period. NOTE: VIX floor not applied (no historical VIX).\n")

    if args.sweep:
        print("Running parameter sweep (60 combinations × 2 folds = 120 backtests)...")
        results = parameter_sweep(rows)
        split_pct = 30
        print(f"\nPARAMETER SWEEP RESULTS (sorted by out-of-sample Sharpe)")
        print(f"Out-of-sample period: last {split_pct}% of data.")
        print(f"Sharpe differences < 0.3 between parameter sets are likely noise at this sample size.")
        print(f"{'STOP':>6} {'LTP':>6} {'DECAY':>7} | "
              f"{'IS Sharpe':>10} {'OOS Sharpe':>10} | "
              f"{'OOS P&L':>10} {'Overfit':>8}")
        print("-" * 72)
        current = {"STOP_MULT": STOP_MULT, "MIN_LTP": MIN_LTP, "TARGET_DECAY": TARGET_DECAY}
        for r in results[:10]:
            flag = " OVERFIT" if r["is_overfit"] else ""
            is_cur = all(abs(r["params"].get(k, 0) - current.get(k, 0)) < 0.01 for k in current)
            marker = " ← CURRENT" if is_cur else ""
            print(f"{r['params'].get('STOP_MULT', 0):>6.2f} "
                  f"{r['params'].get('MIN_LTP', 0):>6.0f} "
                  f"{r['params'].get('TARGET_DECAY', 0):>7.2f} | "
                  f"{r['in_sample_sharpe']:>10.2f} "
                  f"{r['out_sample_sharpe']:>10.2f} | "
                  f"Rs{r['out_sample_net_pnl']:>8,.0f} "
                  f"{r['overfit_ratio']:>7.1f}x{flag}{marker}")

        if args.export:
            _export_backtest_excel({"trades": [], "stats": {}, "parameter_set": {}, "by_exit_reason": {}}, args.export)
    else:
        result = run_backtest(rows, {"lots": args.lots})
        trades = result["trades"]
        stats  = result["stats"]

        print(f"BACKTEST RESULTS ({result['days_tested']} days, "
              f"{len(trades)} trades, {args.lots} lot{'s' if args.lots > 1 else ''})")
        print(f"Coverage: {result['coverage_pct']:.0f}% of days had trades\n")

        if stats.get("insufficient_data"):
            print(f"Insufficient trades for statistics (need 3+, got {stats.get('n', 0)})")
        else:
            total_pnl = sum(t["net_pnl"] for t in trades)
            print(f"Win rate:       {stats['win_rate']:.1f}%")
            print(f"Avg win:       Rs{stats['avg_win']:,.0f}")
            print(f"Avg loss:      Rs{stats['avg_loss']:,.0f}")
            print(f"Edge/trade:    Rs{stats['edge_per_trade']:,.0f}")
            print(f"Kelly:          {stats['kelly_pct']:.1f}%")
            print(f"Half-Kelly:     {stats['half_kelly_pct']:.1f}%")
            print(f"Sharpe:         {stats['sharpe']:.2f}")
            print(f"Total net P&L: Rs{total_pnl:,.0f}")

            print(f"\nBy side:")
            for side in ("CE", "PE"):
                s = result["by_side"].get(side, {})
                if not s.get("insufficient_data"):
                    print(f"  {side}: {s['win_rate']:.0f}% wins  "
                          f"Sharpe {s['sharpe']:.2f}  "
                          f"edge Rs{s['edge_per_trade']:,.0f}/trade  "
                          f"({s['n']} trades)")

            print(f"\nExit breakdown:")
            for reason, data in result["by_exit_reason"].items():
                print(f"  {reason:8s}: {data['count']:3d} trades ({data['pct']:.0f}%)  "
                      f"avg Rs{data['avg_pnl']:,.0f}")

        if args.export:
            _export_backtest_excel(result, args.export)
            print(f"\nExported to {args.export}")

    print(f"\nDone.")

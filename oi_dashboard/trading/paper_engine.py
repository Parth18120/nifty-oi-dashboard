"""
paper_engine.py
Paper-trading helper functions: round-trip transaction costs, signal scanning (used by the
backtester), attribution and strike selection.

Live trade state is held in oi_dashboard.core.state; entry/exit orchestration lives in
oi_dashboard.trading.auto_entry and oi_dashboard.trading.strategies.
"""
import math
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

try:
    from config import (
        COSTS,
        DANGER_ZONE_MAX_OTM,
        IV_PREMIUM_THRESHOLD_PP,
        LOT_SIZE,
        MAX_DISTANCE,
        MIN_DIST_SD_CE,
        MIN_DIST_SD_PE,
        MIN_DISTANCE,
        MIN_LTP,
        OVR_SIGNAL_CONSIDER,
        OVR_SIGNAL_SLIGHT,
        OVR_SIGNAL_STRONG,
        STOP_MULT,
        STRUCTURAL_IV_PREMIUM_PP,
        SWEET_ZONE_BY_DTE,
    )
except ImportError:
    LOT_SIZE                  = 65
    COSTS                     = {}
    STRUCTURAL_IV_PREMIUM_PP  = 0.03
    SWEET_ZONE_BY_DTE         = {}
    DANGER_ZONE_MAX_OTM       = 300
    OVR_SIGNAL_STRONG         = 30
    OVR_SIGNAL_CONSIDER       = 15
    OVR_SIGNAL_SLIGHT         = 5
    IV_PREMIUM_THRESHOLD_PP   = 1.5
    MIN_LTP                   = 15.0
    MAX_DISTANCE              = 800
    MIN_DISTANCE              = 300
    MIN_DIST_SD_CE            = 1.5
    MIN_DIST_SD_PE            = 1.8
    STOP_MULT                 = 1.5

try:
    from oi_dashboard.analytics import vol_engine as V
except ImportError:
    V = None


# ── DTE-adjusted stop multiplier ──────────────────────────────────────────────

def dte_adjusted_stop_mult(dte: float) -> float:
    """
    Tighter stop near expiry where gamma is explosive.
    DTE < 2:  1.25x  DTE 2-4:  1.50x  DTE 4-6:  1.75x  DTE > 6:  2.00x
    """
    if dte < 2.0:
        return 1.25
    elif dte < 4.0:
        return 1.50
    elif dte < 6.0:
        return 1.75
    else:
        return 2.00


# ── VIX-adjusted max distance ─────────────────────────────────────────────────

def _vix_adjusted_max_distance(vix: float) -> int:
    """Dynamic MAX_DISTANCE based on VIX regime."""
    if vix <= 0:
        return MAX_DISTANCE
    if vix < 12:
        return 1000
    elif vix < 15:
        return 800
    elif vix < 18:
        return 600
    else:
        return 400


# ── Preferred sell side (skew-driven) ─────────────────────────────────────────

def _preferred_sell_side(skew_data: dict, sigma: float) -> str:
    """
    Returns "CE", "PE", or "BOTH" based on which side has better edge.
    skew_data: dict from surface_engine with keys: skew_rr25, atm_iv, pe_iv
    """
    if not skew_data:
        return "PE"

    rr25      = skew_data.get("skew_rr25", 0) or 0
    ce_iv     = skew_data.get("atm_iv", sigma) or sigma
    pe_iv     = skew_data.get("pe_iv") or skew_data.get("atm_iv", sigma) or sigma

    ce_premium = ce_iv - sigma
    pe_premium = pe_iv - sigma

    if rr25 < -1.0:
        return "CE"
    if ce_premium >= IV_PREMIUM_THRESHOLD_PP and pe_premium >= IV_PREMIUM_THRESHOLD_PP:
        return "BOTH"
    if ce_premium >= IV_PREMIUM_THRESHOLD_PP and pe_premium < IV_PREMIUM_THRESHOLD_PP:
        return "CE"
    if pe_premium >= IV_PREMIUM_THRESHOLD_PP and ce_premium < IV_PREMIUM_THRESHOLD_PP:
        return "PE"
    return "PE"   # default — puts have structural skew premium most of the time


# ── scan_signals ───────────────────────────────────────────────────────────────

def scan_signals(chain: list, spot: float, sigma: float, dte: int,
                 vix: float = 0.0, skew_data: dict = None) -> tuple:
    """
    Scan the option chain for overpriced sell candidates within the sweet zone.
    Returns (rows_with_signals, best_ce_candidate, best_pe_candidate).

    Primary signal: per-strike IV premium (strike_iv - sigma) using Dhan's real
    implied vols (ce_iv / pe_iv already in each chain row). Positive iv_prem means
    the market is paying more than our vol forecast for this strike — a real edge.

    Fallback to flat-sigma OVR% (ce_rv_realistic_ovr) when Dhan IV is missing/zero.
    This removes the systematic bias where OTM puts always looked overpriced vs flat
    sigma simply because BS ignores skew.

    vix: current India VIX — used for dynamic MAX_DISTANCE.
    skew_data: surface dict — used to determine preferred_sell_side.
    """
    zone    = SWEET_ZONE_BY_DTE.get(max(1, min(7, dte)), (400, 600))
    otm_min = max(zone[0], DANGER_ZONE_MAX_OTM)

    # Dynamic max distance by VIX regime
    max_dist_effective = _vix_adjusted_max_distance(vix)

    # SD-based minimum distance (scales with vol and DTE — avoids fixed-point blindness)
    daily_sigma_pts = spot * (sigma / 100.0) / math.sqrt(252)
    dte_sigma_pts   = daily_sigma_pts * math.sqrt(max(dte, 0.5))

    # sigma_pct: vol_engine stores sigma as decimal (e.g. 0.09); chain may use either.
    # sigma passed in from the server is already in percent (e.g. 9.0).
    sigma_pct = sigma   # percent units, matching ce_iv / pe_iv in chain

    ce_candidates = []
    pe_candidates = []

    for row in chain:
        s      = row["strike"]
        ce_otm = s - spot
        pe_otm = spot - s
        ce_ltp = float(row.get("ce_ltp") or 0)
        pe_ltp = float(row.get("pe_ltp") or 0)

        for side, otm, ltp in (("CE", ce_otm, ce_ltp), ("PE", pe_otm, pe_ltp)):
            if otm <= 0 or ltp < MIN_LTP:
                continue

            # Per-side SD minimum distance
            min_sd = MIN_DIST_SD_CE if side == "CE" else MIN_DIST_SD_PE
            min_dist_sd_pts = dte_sigma_pts * min_sd
            min_dist_effective = max(MIN_DISTANCE, min_dist_sd_pts)

            dist_sd = round(otm / dte_sigma_pts, 2) if dte_sigma_pts > 0 else 0.0

            if otm < min_dist_effective or otm > max_dist_effective:
                continue

            tag        = side.lower()
            strike_iv  = float(row.get(f"{tag}_iv") or 0)
            ovr_method = "flat_sigma"
            iv_prem    = None

            if strike_iv > 1.0 and V is not None:
                # Primary path: real per-strike IV from Dhan
                iv_prem, iv_valid = V.strike_iv_premium(strike_iv, sigma_pct)
                if iv_valid:
                    ovr        = round(iv_prem / sigma_pct * 100, 1) if sigma_pct > 0 else 0.0
                    fair       = V.bs_price(spot, s, strike_iv / 100.0, dte, side)
                    ovr_method = "iv_prem"
                    qualifies  = iv_prem >= IV_PREMIUM_THRESHOLD_PP
                else:
                    # iv_prem invalid — fall through to flat sigma
                    strike_iv = None
                    iv_prem   = None

            if strike_iv is None or strike_iv <= 1.0 or ovr_method == "flat_sigma":
                # Fallback: flat sigma OVR% (old method — structurally biased for OTM puts)
                ovr_key  = f"{tag}_rv_realistic_ovr"
                fair_key = f"{tag}_rv_realistic_fair"
                ovr      = row.get(ovr_key) or row.get(f"{tag}_rv_ovr")
                fair     = row.get(fair_key) or row.get(f"{tag}_rv_fair") or 0.0
                if ovr is None or ovr < OVR_SIGNAL_CONSIDER:
                    continue
                qualifies  = ovr >= OVR_SIGNAL_CONSIDER
                ovr_method = "flat_sigma"

            if not qualifies:
                continue

            if ovr is None:
                continue

            label = ("STRONG"  if (iv_prem is not None and iv_prem >= IV_PREMIUM_THRESHOLD_PP * 2)
                               or (iv_prem is None     and ovr >= OVR_SIGNAL_STRONG)
                     else "CONSIDER" if (iv_prem is not None and iv_prem >= IV_PREMIUM_THRESHOLD_PP)
                               or (iv_prem is None     and ovr >= OVR_SIGNAL_CONSIDER)
                     else "SLIGHT")

            candidate = {
                "strike":      s,
                "side":        side,
                "ltp":         ltp,
                "ovr":         ovr,
                "otm":         otm,
                "dist_sd":     dist_sd,
                "label":       label,
                "iv_prem":     iv_prem,
                "strike_iv":   strike_iv if strike_iv and strike_iv > 1.0 else None,
                "fair":        round(fair, 2) if fair else None,
                "ovr_method":  ovr_method,
            }
            # Add iv_prem to the correct side key for API response
            if side == "CE":
                candidate["ce_iv_prem"]   = iv_prem
                candidate["ce_strike_iv"] = candidate["strike_iv"]
                candidate["pe_iv_prem"]   = None
                candidate["pe_strike_iv"] = None
                ce_candidates.append(candidate)
            else:
                candidate["pe_iv_prem"]   = iv_prem
                candidate["pe_strike_iv"] = candidate["strike_iv"]
                candidate["ce_iv_prem"]   = None
                candidate["ce_strike_iv"] = None
                pe_candidates.append(candidate)

    # Sort by iv_prem (primary) when available; fall back to ovr/100 for flat-sigma rows
    def _sort_key(x):
        p = x.get("iv_prem")
        return p if p is not None else x.get("ovr", 0) / 100.0

    ce_candidates.sort(key=_sort_key, reverse=True)
    pe_candidates.sort(key=_sort_key, reverse=True)

    best_ce = ce_candidates[0] if ce_candidates else None
    best_pe = pe_candidates[0] if pe_candidates else None
    preferred = _preferred_sell_side(skew_data or {}, sigma_pct)
    return ce_candidates + pe_candidates, best_ce, best_pe, preferred


# ── round_trip_costs ───────────────────────────────────────────────────────────

def round_trip_costs(sell_px: float, buy_px: float, lots: int,
                     trade_type: str = "STRANGLE") -> float:
    """
    Full round-trip transaction costs using COSTS dict from config.py.
    Identical logic to trading.accounting.compute_trade_costs() but as a standalone function.

    sell_px: net credit per share at entry (total of all sell legs)
    buy_px:  net debit per share at exit  (total of all buy-back legs)
    """
    lot_size = LOT_SIZE
    units    = lots * lot_size

    if trade_type == "IC":
        n_orders = 8
    elif trade_type == "GAP_SPREAD":
        n_orders = 2
    else:
        n_orders = 4   # strangle / spread: 2 legs × entry + exit

    sell_turnover = sell_px * units
    buy_turnover  = buy_px  * units
    turnover      = sell_turnover + buy_turnover

    brokerage = COSTS.get("brokerage_per_order", 20) * n_orders
    exch_fee  = turnover * COSTS.get("exchange_txn_pct", 0.053) / 100
    sebi_fee  = turnover * COSTS.get("sebi_pct", 0.0001) / 100
    stt       = sell_turnover * COSTS.get("stt_sell_premium_pct", 0.0625) / 100
    stamp     = buy_turnover  * COSTS.get("stamp_duty_pct", 0.003) / 100
    gst       = (brokerage + exch_fee) * COSTS.get("gst_pct", 18.0) / 100

    return round(brokerage + exch_fee + sebi_fee + stt + stamp + gst, 2)


# ── classify_market ────────────────────────────────────────────────────────────

def classify_market(spot: float, spot_data: dict, chain: list,
                    sigma: float, dte: int) -> str:
    """
    Thin wrapper around trading.classification.classify_market (which owns the state).
    Use this for backtest.py where the live server is not running.
    """
    try:
        from oi_dashboard.trading.classification import classify_market as _live_classify
        return _live_classify(spot_data, chain, spot)
    except Exception:
        # Fallback for backtest (no live server)
        gap_pct = 0.0
        pc = float(spot_data.get("prev_close", 0) or 0)
        op = float(spot_data.get("open", pc) or pc)
        if pc > 0:
            gap_pct = (op - pc) / pc * 100

        if abs(gap_pct) > 0.3:
            return "GAP_UP" if gap_pct > 0 else "GAP_DOWN"

        for row in chain:
            if float(row.get("pe_rv_ovr") or 0) > 30 or float(row.get("ce_rv_ovr") or 0) > 30:
                return "OVERPRICED"

        rng = max(r.get("ce_ltp", 0) for r in chain) if chain else 0
        return "RANGE" if rng < 150 else "SKIP"


# ── day_summary ────────────────────────────────────────────────────────────────

def day_summary(trades: list, date: str = None) -> dict:
    """
    Compute P&L summary for a given date (or today if None).
    trades: list of trade dicts (from db.get_trades() or _paper_trades).
    """
    if date is None:
        date = datetime.now(IST).strftime("%Y-%m-%d")

    day_trades = [t for t in trades if (t.get("trade_date") or "") == date]
    closed     = [t for t in day_trades if t.get("status") != "OPEN"]
    open_trades = [t for t in day_trades if t.get("status") == "OPEN"]

    gross_r = sum(t.get("pnl_total", 0) or 0 for t in closed)
    net_r   = sum(t.get("pnl_net",   0) or 0 for t in closed)
    gross_u = sum(t.get("pnl_total", 0) or 0 for t in open_trades)

    wins  = [t for t in closed if (t.get("pnl_net") or 0) > 0]
    loss  = [t for t in closed if (t.get("pnl_net") or 0) <= 0]

    return {
        "date":           date,
        "total_trades":   len(day_trades),
        "closed":         len(closed),
        "open":           len(open_trades),
        "wins":           len(wins),
        "losses":         len(loss),
        "win_rate":       round(len(wins) / max(len(closed), 1) * 100, 1),
        "gross_realized": round(gross_r, 2),
        "net_realized":   round(net_r, 2),
        "unrealized":     round(gross_u, 2),
        "total_pnl":      round(gross_r + gross_u, 2),
    }


# ── export_tearsheet ───────────────────────────────────────────────────────────

def export_tearsheet(path: str, days: int = 180) -> str:
    """
    Export a 7-sheet professional tearsheet to an Excel file.

    Sheets:
      1. Overview      — KPI dashboard with key metrics
      2. Trade Log     — All closed trades with full attribution
      3. Edge Stats    — Rolling 20/50/all-time windows comparison
      4. Attribution   — Greek P&L breakdown per trade + averages
      5. Regime        — VIX bucket performance analysis
      6. Equity Curve  — Cumulative P&L + drawdown time series
      7. Backtest      — Backtest results vs live for comparison

    Returns path on success, raises on openpyxl missing.
    """
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Border, Font, GradientFill, PatternFill, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        raise ImportError("openpyxl required: pip install openpyxl")

    from oi_dashboard.analytics import analytics_engine as AE
    from oi_dashboard.storage import db

    # ── Load data ─────────────────────────────────────────────────────────────
    trades  = db.get_paper_trades_closed(days=days)
    rs_20   = AE.rolling_edge_stats(trades, window=20)
    rs_50   = AE.rolling_edge_stats(trades, window=50)
    rs_all  = AE.rolling_edge_stats(trades, window=len(trades))
    regime  = AE.regime_conditional_perf(trades)
    eq      = AE.equity_curve(trades)

    # Attribution averages
    attr_cols = ["theta_pnl", "delta_pnl", "vega_pnl", "residual_pnl"]
    attr_avgs = {}
    for col in attr_cols:
        vals = [t[col] for t in trades if t.get(col) is not None]
        attr_avgs[col] = round(sum(vals) / len(vals), 0) if vals else None

    total_net   = sum(t.get("pnl_net") or t.get("net_pnl") or 0 for t in trades)
    total_gross = sum(t.get("pnl_total") or t.get("gross_pnl") or 0 for t in trades)
    total_costs = total_gross - total_net

    generated_at = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")

    # ── Style helpers ─────────────────────────────────────────────────────────
    _HEADER_FILL = PatternFill("solid", fgColor="1E293B")   # dark slate
    _SUBHD_FILL  = PatternFill("solid", fgColor="0F172A")   # darker
    _WIN_FILL    = PatternFill("solid", fgColor="DCFCE7")   # light green
    _LOSS_FILL   = PatternFill("solid", fgColor="FEE2E2")   # light red
    _STOP_FILL   = PatternFill("solid", fgColor="FFEDD5")   # light orange
    _ALT_FILL    = PatternFill("solid", fgColor="F8FAFC")   # light grey alt row
    _WHITE_FILL  = PatternFill("solid", fgColor="FFFFFF")

    _H_FONT  = Font(bold=True, color="FFFFFF", name="Calibri", size=11)
    _SH_FONT = Font(bold=True, color="94A3B8", name="Calibri", size=9)
    _BD_FONT = Font(bold=True, name="Calibri", size=10)
    _NO_FONT = Font(name="Calibri", size=10)
    _SM_FONT = Font(name="Calibri", size=9, color="64748B")
    _GR_FONT = Font(bold=True, name="Calibri", size=10, color="16A34A")
    _RD_FONT = Font(bold=True, name="Calibri", size=10, color="DC2626")

    _THIN = Side(style="thin", color="CBD5E1")
    _BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
    _BB = Border(bottom=Side(style="medium", color="1E293B"))

    def _set_header(ws, row: int, cols: list, width: int = 14):
        for i, label in enumerate(cols, start=1):
            c = ws.cell(row=row, column=i, value=label)
            c.fill = _HEADER_FILL
            c.font = _H_FONT
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            c.border = _BORDER
            ws.column_dimensions[get_column_letter(i)].width = width

    def _kpi(ws, row: int, label: str, value, color_fn=None, raw_val=None):
        """Write a labelled KPI row. raw_val is passed to color_fn if provided."""
        lc = ws.cell(row=row, column=1, value=label)
        lc.font = _SM_FONT
        lc.alignment = Alignment(horizontal="left")
        vc = ws.cell(row=row, column=2, value=value)
        vc.font = _BD_FONT
        if color_fn and raw_val is not None:
            try:
                vc.font = color_fn(raw_val)
            except Exception:
                pass
        ws.row_dimensions[row].height = 18

    def _pnl_font(v):
        return _GR_FONT if (v or 0) >= 0 else _RD_FONT

    def _sharpe_font(v):
        return _GR_FONT if (v or 0) >= 1.0 else (_BD_FONT if (v or 0) >= 0 else _RD_FONT)

    def _wr_font(v):
        return _GR_FONT if (v or 0) >= 65 else (_BD_FONT if (v or 0) >= 50 else _RD_FONT)

    def _alt_fill(row_idx):
        return _ALT_FILL if row_idx % 2 == 0 else _WHITE_FILL

    wb = openpyxl.Workbook()

    # ═══════════════════════════════════════════════════════════════════════════
    # Sheet 1 — Overview
    # ═══════════════════════════════════════════════════════════════════════════
    ws1 = wb.active
    ws1.title = "Overview"
    ws1.column_dimensions["A"].width = 28
    ws1.column_dimensions["B"].width = 20
    ws1.column_dimensions["C"].width = 5
    ws1.column_dimensions["D"].width = 28
    ws1.column_dimensions["E"].width = 20

    # Title
    t = ws1.cell(row=1, column=1, value="NIFTY OPTIONS STRATEGY — PERFORMANCE TEARSHEET")
    t.font = Font(bold=True, name="Calibri", size=14, color="1E293B")
    ws1.merge_cells("A1:E1")
    ws1.row_dimensions[1].height = 30

    sub = ws1.cell(row=2, column=1, value=f"Generated: {generated_at}  |  Period: last {days} days  |  Lot size: {LOT_SIZE}")
    sub.font = _SM_FONT
    ws1.merge_cells("A2:E2")
    ws1.row_dimensions[2].height = 16

    # Left block: Performance
    r = 4
    hdr = ws1.cell(row=r, column=1, value="PERFORMANCE")
    hdr.font = Font(bold=True, color="1E293B", name="Calibri"); hdr.fill = PatternFill("solid", fgColor="E2E8F0")
    ws1.merge_cells(f"A{r}:B{r}")
    r += 1

    n_closed = len(trades)
    insuff = rs_all.get('insufficient_data', True)
    _kpi(ws1, r,   "Total closed trades",  n_closed); r += 1
    _kpi(ws1, r,   "Win rate",    f"{rs_all.get('win_rate',0):.1f}%" if not insuff else "—",
         _wr_font,    rs_all.get('win_rate') if not insuff else None); r += 1
    _kpi(ws1, r,   "Sharpe (all-time)",    rs_all.get('sharpe') if not insuff else "—",
         _sharpe_font, rs_all.get('sharpe') if not insuff else None); r += 1
    _kpi(ws1, r,   "Edge per trade",       f"₹{rs_all.get('edge_per_trade',0):,.0f}" if not insuff else "—",
         _pnl_font,   rs_all.get('edge_per_trade') if not insuff else None); r += 1
    _kpi(ws1, r,   "Kelly fraction",       f"{rs_all.get('kelly_pct',0):.1f}%" if not insuff else "—"); r += 1
    _kpi(ws1, r,   "Half-Kelly",           f"{rs_all.get('half_kelly_pct',0):.1f}%" if not insuff else "—"); r += 1
    _kpi(ws1, r,   "Avg win",              f"₹{rs_all.get('avg_win',0):,.0f}" if not insuff else "—"); r += 1
    _kpi(ws1, r,   "Avg loss",             f"₹{rs_all.get('avg_loss',0):,.0f}" if not insuff else "—"); r += 1
    _kpi(ws1, r,   "Longest win streak",   rs_all.get('longest_win_streak',"—") if not insuff else "—"); r += 1
    _kpi(ws1, r,   "Longest loss streak",  rs_all.get('longest_loss_streak',"—") if not insuff else "—"); r += 1

    # Left block: P&L summary
    r += 1
    hdr2 = ws1.cell(row=r, column=1, value="P&L SUMMARY")
    hdr2.font = Font(bold=True, color="1E293B", name="Calibri"); hdr2.fill = PatternFill("solid", fgColor="E2E8F0")
    ws1.merge_cells(f"A{r}:B{r}")
    r += 1
    _kpi(ws1, r,  "Total gross P&L", f"₹{total_gross:,.0f}", _pnl_font, total_gross); r += 1
    _kpi(ws1, r,  "Total costs",    f"₹{total_costs:,.0f}"); r += 1
    _kpi(ws1, r,  "Total net P&L",  f"₹{total_net:,.0f}",   _pnl_font, total_net); r += 1

    peak   = max((p["peak"]        for p in eq), default=0) if eq else 0
    max_dd = min((p["drawdown_rs"] for p in eq), default=0) if eq else 0
    _kpi(ws1, r,  "Peak equity",  f"₹{peak:,.0f}"); r += 1
    _kpi(ws1, r,  "Max drawdown", f"₹{max_dd:,.0f}", _pnl_font, max_dd); r += 1

    # Right block: Attribution averages
    r4 = 4
    hdr3 = ws1.cell(row=r4, column=4, value="ATTRIBUTION (avg/trade)")
    hdr3.font = Font(bold=True, color="1E293B", name="Calibri"); hdr3.fill = PatternFill("solid", fgColor="E2E8F0")
    ws1.merge_cells(f"D{r4}:E{r4}")
    r4 += 1
    for col, label in [("theta_pnl","Theta P&L"), ("delta_pnl","Delta P&L"),
                       ("vega_pnl","Vega P&L"), ("residual_pnl","Residual")]:
        v = attr_avgs.get(col)
        _kpi(ws1, r4, label, f"₹{v:,.0f}" if v is not None else "—", _pnl_font if v is not None else None)
        ws1.cell(row=r4, column=4).font = _SM_FONT
        r4 += 1

    # Right block: Exit mix
    r4 += 1
    hdr4 = ws1.cell(row=r4, column=4, value="EXIT MIX")
    hdr4.font = Font(bold=True, color="1E293B", name="Calibri"); hdr4.fill = PatternFill("solid", fgColor="E2E8F0")
    ws1.merge_cells(f"D{r4}:E{r4}")
    r4 += 1
    for reason in ("STOP", "TARGET", "EOD"):
        grp = [t for t in trades if str(t.get("exit_reason") or "").upper() == reason or
               (reason == "EOD" and str(t.get("exit_reason") or "").upper().startswith("EOD"))]
        pct = round(len(grp) / max(n_closed, 1) * 100, 1)
        _kpi(ws1, r4, f"{reason} exits", f"{len(grp)} ({pct}%)")
        ws1.cell(row=r4, column=4).font = _SM_FONT
        r4 += 1

    # Confidence note
    conf_row = max(r, r4) + 2
    note = ws1.cell(row=conf_row, column=1,
                    value=f"Confidence: {rs_all.get('confidence','—') if not rs_all.get('insufficient_data') else 'INSUFFICIENT DATA (<3 trades)'}")
    note.font = _SM_FONT
    ws1.merge_cells(f"A{conf_row}:E{conf_row}")

    # ═══════════════════════════════════════════════════════════════════════════
    # Sheet 2 — Trade Log
    # ═══════════════════════════════════════════════════════════════════════════
    ws2 = wb.create_sheet("Trade Log")
    tl_cols = ["Date","Side","Strike","Lots","Entry","Exit","Exit Reason",
               "Gross P&L","Costs","Net P&L","Sigma%","Entry OVR%",
               "VIX at Entry","Mode","Entry Reason"]
    _set_header(ws2, 1, tl_cols, width=13)

    for ri, t in enumerate(trades, start=2):
        net   = t.get("pnl_net") or t.get("net_pnl") or 0
        gross = t.get("pnl_total") or t.get("gross_pnl") or 0
        row_data = [
            (t.get("trade_date") or "")[:10],
            t.get("opt_type"),
            t.get("strike"),
            t.get("lots"),
            t.get("entry_ltp"),
            t.get("exit_ltp"),
            t.get("exit_reason"),
            gross,
            (t.get("costs_total") or t.get("costs") or 0),
            net,
            t.get("sigma_used"),
            t.get("entry_ovr"),
            t.get("vix_at_entry"),
            t.get("mode"),
            (t.get("entry_reason") or "")[:60],
        ]
        for ci, val in enumerate(row_data, start=1):
            c = ws2.cell(row=ri, column=ci, value=val)
            c.font = _NO_FONT
            c.border = _BORDER
            c.alignment = Alignment(horizontal="right" if ci > 4 else "left")

        reason = str(t.get("exit_reason") or "").upper()
        row_fill = (_STOP_FILL if reason == "STOP"
                    else _WIN_FILL if net > 0
                    else _LOSS_FILL)
        for ci in range(1, len(tl_cols) + 1):
            ws2.cell(row=ri, column=ci).fill = row_fill

    # Freeze header
    ws2.freeze_panes = "A2"

    # ═══════════════════════════════════════════════════════════════════════════
    # Sheet 3 — Edge Stats
    # ═══════════════════════════════════════════════════════════════════════════
    ws3 = wb.create_sheet("Edge Stats")
    ws3.column_dimensions["A"].width = 26
    ws3.column_dimensions["B"].width = 14
    ws3.column_dimensions["C"].width = 14
    ws3.column_dimensions["D"].width = 14

    title3 = ws3.cell(row=1, column=1, value="ROLLING EDGE STATISTICS")
    title3.font = Font(bold=True, name="Calibri", size=13, color="1E293B")
    ws3.merge_cells("A1:D1")

    ws3.cell(row=2, column=2, value="Last 20 trades").font = _BD_FONT
    ws3.cell(row=2, column=3, value="Last 50 trades").font = _BD_FONT
    ws3.cell(row=2, column=4, value="All time").font = _BD_FONT
    for ci in range(1, 5):
        ws3.cell(row=2, column=ci).fill = _HEADER_FILL
        ws3.cell(row=2, column=ci).font = _H_FONT
        ws3.cell(row=2, column=ci).alignment = Alignment(horizontal="center")

    metrics = [
        ("Trades (n)",        "n"),
        ("Win rate %",        "win_rate"),
        ("Avg win (₹)",       "avg_win"),
        ("Avg loss (₹)",      "avg_loss"),
        ("Edge/trade (₹)",    "edge_per_trade"),
        ("Kelly %",           "kelly_pct"),
        ("Half-Kelly %",      "half_kelly_pct"),
        ("Sharpe",            "sharpe"),
        ("Longest win streak","longest_win_streak"),
        ("Longest loss streak","longest_loss_streak"),
        ("Current streak",    "current_streak"),
        ("STOP exits %",      "pct_stopped"),
        ("TARGET exits %",    "pct_targeted"),
        ("EOD exits %",       "pct_eod"),
        ("Confidence",        "confidence"),
    ]
    for ri, (label, key) in enumerate(metrics, start=3):
        ws3.cell(row=ri, column=1, value=label).font = _SM_FONT
        for ci, rs in enumerate([rs_20, rs_50, rs_all], start=2):
            if rs.get("insufficient_data"):
                val = "—"
            else:
                val = rs.get(key, "—")
                if isinstance(val, float):
                    val = round(val, 2)
            cell = ws3.cell(row=ri, column=ci, value=val)
            cell.alignment = Alignment(horizontal="center")
            cell.fill = _alt_fill(ri)
            cell.font = _NO_FONT

    # ═══════════════════════════════════════════════════════════════════════════
    # Sheet 4 — Attribution
    # ═══════════════════════════════════════════════════════════════════════════
    ws4 = wb.create_sheet("Attribution")
    attr_cols_hdr = ["Date","Side","Strike","Theta P&L","Delta P&L",
                     "Vega P&L","Residual","Gross P&L","Net P&L",
                     "Verdict","Days Held","IV at Entry","IV at Exit"]
    _set_header(ws4, 1, attr_cols_hdr, width=13)

    for ri, t in enumerate(trades, start=2):
        net   = t.get("pnl_net") or t.get("net_pnl") or 0
        gross = t.get("pnl_total") or t.get("gross_pnl") or 0
        row_data = [
            (t.get("trade_date") or "")[:10],
            t.get("opt_type"),
            t.get("strike"),
            t.get("theta_pnl"),
            t.get("delta_pnl"),
            t.get("vega_pnl"),
            t.get("residual_pnl"),
            gross, net,
            "",   # verdict (computed below per trade if needed)
            t.get("days_held"),
            t.get("iv_at_entry"),
            t.get("iv_at_exit"),
        ]
        for ci, val in enumerate(row_data, start=1):
            c = ws4.cell(row=ri, column=ci, value=val)
            c.font = _NO_FONT
            c.fill = _alt_fill(ri)
            c.border = _BORDER
            c.alignment = Alignment(horizontal="right" if ci > 3 else "left")

    # Averages row
    avg_row = len(trades) + 3
    ws4.cell(row=avg_row, column=1, value="AVERAGE").font = _BD_FONT
    for ci, col in enumerate(["theta_pnl","delta_pnl","vega_pnl","residual_pnl"], start=4):
        val = attr_avgs.get(col)
        c = ws4.cell(row=avg_row, column=ci, value=val)
        c.font = _GR_FONT if (val or 0) >= 0 else _RD_FONT
    ws4.freeze_panes = "A2"

    # ═══════════════════════════════════════════════════════════════════════════
    # Sheet 5 — Regime
    # ═══════════════════════════════════════════════════════════════════════════
    ws5 = wb.create_sheet("Regime")
    reg_hdr = ["Regime Bucket","Trades","Win Rate %","Avg Net P&L","Sharpe","Assessment"]
    _set_header(ws5, 1, reg_hdr, width=16)

    title5 = ws5.cell(row=1, column=1, value="REGIME-CONDITIONAL PERFORMANCE (by VIX z-score)")
    title5.fill = _HEADER_FILL; title5.font = _H_FONT
    ws5.merge_cells("A1:F1")

    _set_header(ws5, 2, reg_hdr, width=16)
    assessments = {"Calm": "Best zone — trade full size",
                   "Normal": "Standard zone — trade normal size",
                   "Elevated": "Caution — reduce size",
                   "High": "High stress — avoid or hedge",
                   "Unknown": "VIX not recorded at entry"}
    for ri, b in enumerate(regime, start=3):
        sharpe = b.get("sharpe", 0)
        assess = assessments.get(b["bucket"], "")
        row_data = [b["bucket"], b["count"], b["win_rate"],
                    b["avg_net_pnl"], sharpe, assess]
        fill = _WIN_FILL if sharpe >= 1.0 else (_LOSS_FILL if sharpe < 0 else _ALT_FILL)
        for ci, val in enumerate(row_data, start=1):
            c = ws5.cell(row=ri, column=ci, value=val)
            c.font = _NO_FONT; c.fill = fill; c.border = _BORDER
            c.alignment = Alignment(horizontal="center" if ci > 1 else "left")

    if not regime:
        ws5.cell(row=3, column=1, value="No regime data — vix_at_entry not yet recorded for closed trades")
        ws5.cell(row=3, column=1).font = _SM_FONT

    # ═══════════════════════════════════════════════════════════════════════════
    # Sheet 6 — Equity Curve
    # ═══════════════════════════════════════════════════════════════════════════
    ws6 = wb.create_sheet("Equity Curve")
    eq_hdr = ["Date","Trade ID","Net P&L","Cum P&L","Peak","Drawdown (₹)","Drawdown %"]
    _set_header(ws6, 1, eq_hdr, width=14)

    for ri, pt in enumerate(eq, start=2):
        row_data = [
            pt.get("date"), pt.get("trade_id"),
            pt.get("net_pnl"), pt.get("cum_pnl"),
            pt.get("peak"), pt.get("drawdown_rs"), pt.get("drawdown_pct"),
        ]
        dd = pt.get("drawdown_rs", 0)
        fill = _LOSS_FILL if (dd or 0) < -500 else _ALT_FILL if ri % 2 == 0 else _WHITE_FILL
        for ci, val in enumerate(row_data, start=1):
            c = ws6.cell(row=ri, column=ci, value=val)
            c.font = _NO_FONT; c.fill = fill; c.border = _BORDER
            c.alignment = Alignment(horizontal="right" if ci > 2 else "left")

    if not eq:
        ws6.cell(row=2, column=1, value="No closed trades yet — equity curve will populate as trades close")
        ws6.cell(row=2, column=1).font = _SM_FONT

    ws6.freeze_panes = "A2"

    # ═══════════════════════════════════════════════════════════════════════════
    # Sheet 7 — Backtest
    # ═══════════════════════════════════════════════════════════════════════════
    ws7 = wb.create_sheet("Backtest")
    ws7.column_dimensions["A"].width = 30
    ws7.column_dimensions["B"].width = 18
    ws7.column_dimensions["C"].width = 5
    ws7.column_dimensions["D"].width = 30
    ws7.column_dimensions["E"].width = 18

    title7 = ws7.cell(row=1, column=1, value="BACKTEST SUMMARY (synthetic BS simulation)")
    title7.font = Font(bold=True, name="Calibri", size=13, color="1E293B")
    ws7.merge_cells("A1:E1")

    try:
        from oi_dashboard.trading import backtest as BT
        bt_rows  = db.get_spot_daily(days + 25)
        bt_res   = BT.run_backtest(bt_rows)
        bt_stats = bt_res.get("stats", {})
        bt_prem  = BT.BT_IV_PREMIUM_PP

        note7 = ws7.cell(row=2, column=1,
                         value=f"Entry prices use realized vol + {bt_prem:.1f}pp synthetic IV premium. "
                               f"VIX floor not applied. {bt_res.get('days_tested',0)} days tested.")
        note7.font = _SM_FONT
        ws7.merge_cells("A2:E2")

        bt_metrics = [
            ("Days tested",       bt_res.get("days_tested","")),
            ("Total trades",      len(bt_res.get("trades",[]))),
            ("Coverage %",        bt_res.get("coverage_pct","")),
            ("Win rate %",        bt_stats.get("win_rate","") if not bt_stats.get("insufficient_data") else "—"),
            ("Avg win (₹)",       bt_stats.get("avg_win","")  if not bt_stats.get("insufficient_data") else "—"),
            ("Avg loss (₹)",      bt_stats.get("avg_loss","") if not bt_stats.get("insufficient_data") else "—"),
            ("Edge/trade (₹)",    bt_stats.get("edge_per_trade","") if not bt_stats.get("insufficient_data") else "—"),
            ("Sharpe",            bt_stats.get("sharpe","")   if not bt_stats.get("insufficient_data") else "—"),
            ("CE Sharpe",         (bt_res.get("by_side",{}).get("CE",{}).get("sharpe","") or "—")),
            ("PE Sharpe",         (bt_res.get("by_side",{}).get("PE",{}).get("sharpe","") or "—")),
        ]
        for ri, (label, val) in enumerate(bt_metrics, start=4):
            ws7.cell(row=ri, column=1, value=label).font = _SM_FONT
            vc = ws7.cell(row=ri, column=2, value=val)
            vc.font = _BD_FONT

        # Side-by-side: live vs backtest
        ws7.cell(row=4,  column=4, value="LIVE vs BACKTEST COMPARISON").font = Font(bold=True, name="Calibri")
        ws7.cell(row=5,  column=4, value="Win rate").font = _SM_FONT
        ws7.cell(row=5,  column=5, value=f"Live: {rs_all.get('win_rate','—') if not rs_all.get('insufficient_data') else '—'}%  |  BT: {bt_stats.get('win_rate','—') if not bt_stats.get('insufficient_data') else '—'}%").font = _BD_FONT
        ws7.cell(row=6,  column=4, value="Sharpe").font = _SM_FONT
        ws7.cell(row=6,  column=5, value=f"Live: {rs_all.get('sharpe','—') if not rs_all.get('insufficient_data') else '—'}  |  BT: {bt_stats.get('sharpe','—') if not bt_stats.get('insufficient_data') else '—'}").font = _BD_FONT

        # Stop breakdown
        for ri, (reason, data) in enumerate(bt_res.get("by_exit_reason", {}).items(), start=8):
            ws7.cell(row=ri, column=4, value=f"{reason} exits").font = _SM_FONT
            ws7.cell(row=ri, column=5, value=f"{data['count']} ({data['pct']}%) avg ₹{data['avg_pnl']:,.0f}").font = _BD_FONT

        caveat = ws7.cell(row=20, column=1,
                          value="CAVEAT: Backtest uses daily OHLC extremes as worst-case intraday check. "
                                "Real intraday monitoring would not always be hit at the absolute high/low. "
                                "Results are pessimistic upper-bound on stop frequency.")
        caveat.font = Font(italic=True, name="Calibri", size=9, color="64748B")
        ws7.merge_cells("A20:E20")
    except Exception as bt_err:
        ws7.cell(row=3, column=1, value=f"Backtest data unavailable: {bt_err}").font = _SM_FONT

    # ── Save ──────────────────────────────────────────────────────────────────
    wb.save(path)
    return path

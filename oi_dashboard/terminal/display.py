"""
display.py
Rich terminal dashboard rendering.
ASCII-only output for Windows cp1252 compatibility.
No Unicode box drawing, no rupee symbol (use Rs.), no arrows (use ^ and v).
"""

from datetime import datetime, timedelta, timezone

from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

IST     = timezone(timedelta(hours=5, minutes=30))
console = Console(force_terminal=True)

LOI_SCALE = 100_000.0  # 1 lakh


def _fmt_oi(val):
    """Format OI in lakhs: 450000 -> 4.5L"""
    if val == 0:
        return "0"
    return f"{val / LOI_SCALE:.1f}L"


def _fmt_chg(val):
    """Format OI change with sign and ^ / v symbol."""
    if val > 0:
        return f"+{val / LOI_SCALE:.1f}L ^"
    elif val < 0:
        return f"{val / LOI_SCALE:.1f}L v"
    else:
        return "0"


def _chg_color(val):
    if val > 100_000:
        return "bright_green"
    if val > 0:
        return "green"
    if val < -100_000:
        return "bright_red"
    if val < 0:
        return "red"
    return "grey50"


def _pnl_color(val):
    return "green" if val >= 0 else "red"


# -----------------------------------------------------------------------
# Panel 1: Header bar
# -----------------------------------------------------------------------
def make_header(data, remaining_secs):
    spot    = data["spot"]
    vix     = data["vix"]
    pcr     = data["pcr"]
    pcrlbl  = data["pcr_label"]
    mp      = data["max_pain"]
    now     = datetime.now(IST).strftime("%H:%M:%S")
    dte     = data["dte"]

    chg_col = "green" if spot["change"] >= 0 else "red"
    chg_str = f"{spot['change']:+.2f} ({spot['change_pct']:+.2f}%)"

    market_status = ""
    if spot.get("ltp", 0) == 0:
        market_status = "  [yellow][MARKET CLOSED -- last data][/yellow]"

    header_text = (
        f"[bold cyan]Nifty OI Dashboard[/bold cyan]  |  "
        f"[bold white]Nifty:[/bold white] {spot['ltp']:,.2f}  "
        f"[{chg_col}]{chg_str}[/{chg_col}]  |  "
        f"[bold]VIX:[/bold] {vix:.2f}  |  "
        f"[bold]PCR:[/bold] {pcr:.2f} {pcrlbl.split(' --')[0]}  |  "
        f"[bold]Max Pain:[/bold] {mp:,}  |  "
        f"[bold]DTE:[/bold] {dte}  |  "
        f"[bold]{now} IST[/bold]  |  "
        f"Refresh in: {remaining_secs}s"
        f"{market_status}"
    )
    return Panel(header_text, box=box.SIMPLE, padding=(0, 1))


# -----------------------------------------------------------------------
# Panel 2: Option chain table
# -----------------------------------------------------------------------
def make_chain_table(data):
    chain   = data["chain"]
    spot    = data["spot"]["ltp"]
    atm     = round(spot / 50) * 50

    from config import MY_POSITIONS
    sold_strikes = {p["strike"] for p in MY_POSITIONS}

    tbl = Table(
        title="Option Chain",
        box=box.SIMPLE_HEAVY,
        show_lines=False,
        padding=(0, 1),
        title_style="bold white",
        expand=True,
    )

    # CE columns (cyan header)
    tbl.add_column("CE OI",  style="cyan",   header_style="bold cyan",  justify="right", min_width=6)
    tbl.add_column("CE Chg", style="cyan",   header_style="bold cyan",  justify="right", min_width=8)
    tbl.add_column("CE Vol", style="cyan",   header_style="bold cyan",  justify="right", min_width=5)
    tbl.add_column("CE IV",  style="cyan",   header_style="bold cyan",  justify="right", min_width=5)
    tbl.add_column("CE LTP", style="cyan",   header_style="bold cyan",  justify="right", min_width=7)
    # Strike centre
    tbl.add_column("Strike", style="white",  header_style="bold white", justify="center", min_width=7)
    tbl.add_column("",       style="white",  header_style="",           justify="center", min_width=3)
    # PE columns (magenta header)
    tbl.add_column("PE LTP", style="magenta", header_style="bold magenta", justify="right", min_width=7)
    tbl.add_column("PE IV",  style="magenta", header_style="bold magenta", justify="right", min_width=5)
    tbl.add_column("PE Vol", style="magenta", header_style="bold magenta", justify="right", min_width=5)
    tbl.add_column("PE Chg", style="magenta", header_style="bold magenta", justify="right", min_width=8)
    tbl.add_column("PE OI",  style="magenta", header_style="bold magenta", justify="right", min_width=6)

    for row in chain:
        s        = row["strike"]
        is_atm   = (s == atm)
        is_sold  = (s in sold_strikes)

        ce_chg_col = _chg_color(row["ce_oi_chg"])
        pe_chg_col = _chg_color(row["pe_oi_chg"])

        strike_str = f">>{s}" if is_atm else str(s)
        atm_marker = "ATM" if is_atm else ""

        if is_atm:
            row_style = "bold white"
        elif is_sold:
            row_style = "yellow"
        else:
            row_style = ""

        tbl.add_row(
            _fmt_oi(row["ce_oi"]),
            Text(_fmt_chg(row["ce_oi_chg"]), style=ce_chg_col),
            f"{row['ce_volume']:,}",
            f"{row['ce_iv']:.1f}%",
            f"{row['ce_ltp']:.2f}",
            Text(strike_str, style="bold white" if is_atm else "white"),
            Text(atm_marker, style="bold bright_white"),
            f"{row['pe_ltp']:.2f}",
            f"{row['pe_iv']:.1f}%",
            f"{row['pe_volume']:,}",
            Text(_fmt_chg(row["pe_oi_chg"]), style=pe_chg_col),
            _fmt_oi(row["pe_oi"]),
            style=row_style,
        )

    return Panel(tbl, box=box.ROUNDED, title="[bold]Option Chain[/bold]", padding=(0, 0))


# -----------------------------------------------------------------------
# Panel 3: OI Analysis
# -----------------------------------------------------------------------
def make_oi_analysis(data):
    pcr      = data["pcr"]
    pcrlbl   = data["pcr_label"]
    pcrcol   = data["pcr_color"]
    mp       = data["max_pain"]
    dte      = data["dte"]
    support  = data["support"]
    resist   = data["resist"]
    skew     = data["iv_skew"]
    alerts   = data["oi_alerts"]

    lines = []

    lines.append(f"[bold]PCR:[/bold]      [{pcrcol}]{pcr:.3f}[/{pcrcol}]")
    lines.append(f"[bold]Max Pain:[/bold] {mp:,}")
    lines.append(f"[bold]DTE:[/bold]      {dte} days")
    lines.append("")

    lines.append("[bold cyan]Support (PE OI):[/bold cyan]")
    for i, s in enumerate(support):
        oi_val = next((r["pe_oi"] for r in data["chain"] if r["strike"] == s), 0)
        lines.append(f"  {s:,}  --  {_fmt_oi(oi_val)}")

    lines.append("")
    lines.append("[bold magenta]Resistance (CE OI):[/bold magenta]")
    for r in resist:
        oi_val = next((row["ce_oi"] for row in data["chain"] if row["strike"] == r), 0)
        lines.append(f"  {r:,}  --  {_fmt_oi(oi_val)}")

    lines.append("")
    lines.append("[bold]IV Skew (250pt):[/bold]")
    lines.append(f"  CE {skew['ce_strike']:,}: {skew['ce_iv']:.1f}%")
    lines.append(f"  PE {skew['pe_strike']:,}: {skew['pe_iv']:.1f}%")
    lines.append(f"  [{skew['color']}]{skew['label']}[/{skew['color']}]")

    if alerts:
        lines.append("")
        lines.append("[bold yellow]OI Alerts (this refresh):[/bold yellow]")
        for a in alerts[:5]:
            col = "green" if a["type"] == "BUILDUP" else "red"
            lines.append(f"  [{col}]{a['note']}[/{col}]")

    text = "\n".join(lines)
    return Panel(text, box=box.ROUNDED, title="[bold]OI Analysis[/bold]", padding=(1, 2))


# -----------------------------------------------------------------------
# Panel 4: Greeks / Positions
# -----------------------------------------------------------------------
def make_greeks_panel(data):
    pg = data.get("pos_greeks")
    if not pg:
        return None

    tbl = Table(
        box=box.SIMPLE_HEAVY,
        show_lines=False,
        padding=(0, 1),
        expand=True,
    )
    tbl.add_column("Pos",   style="white",  justify="center", min_width=4)
    tbl.add_column("Strike",style="white",  justify="right",  min_width=6)
    tbl.add_column("Entry", style="white",  justify="right",  min_width=6)
    tbl.add_column("Now",   style="white",  justify="right",  min_width=6)
    tbl.add_column("P&L",   style="white",  justify="right",  min_width=10)
    tbl.add_column("Delta", style="white",  justify="right",  min_width=7)
    tbl.add_column("Theta/day", style="green", justify="right", min_width=10)
    tbl.add_column("Vega/1%",   style="yellow",justify="right", min_width=9)

    for p in pg["breakdown"]:
        pnl_str = f"{'+'if p['unrealised_pnl']>=0 else ''}Rs.{p['unrealised_pnl']:,.0f}"
        pnl_col = _pnl_color(p["unrealised_pnl"])
        theta_str = f"+Rs.{p['theta']:,.0f}" if p["theta"] >= 0 else f"-Rs.{abs(p['theta']):,.0f}"
        vega_str  = f"+Rs.{p['vega']:,.0f}"  if p["vega"] >= 0  else f"-Rs.{abs(p['vega']):,.0f}"

        tbl.add_row(
            f"{p['type']} {p['option']}",
            str(p["strike"]),
            f"{p['entry_price']:.1f}",
            f"{p['current_ltp']:.1f}",
            Text(pnl_str, style=pnl_col),
            f"{p['delta']:+.3f}",
            theta_str,
            vega_str,
        )

    # Net row
    net     = pg
    pnl_tot = sum(p["unrealised_pnl"] for p in pg["breakdown"])
    pnl_tot_str = f"{'+'if pnl_tot>=0 else ''}Rs.{pnl_tot:,.0f}"
    theta_net   = f"+Rs.{net['net_theta']:,.0f}" if net["net_theta"] >= 0 else f"-Rs.{abs(net['net_theta']):,.0f}"
    vega_net    = f"+Rs.{net['net_vega']:,.0f}"  if net["net_vega"] >= 0  else f"-Rs.{abs(net['net_vega']):,.0f}"

    tbl.add_row(
        "[bold]NET[/bold]", "", "", "",
        Text(pnl_tot_str, style=_pnl_color(pnl_tot) + " bold"),
        Text(f"{net['net_delta']:+.3f}", style="bold"),
        Text(theta_net, style="bold green"),
        Text(vega_net,  style="bold yellow"),
    )

    # Interpretation lines
    delta_note = "Delta near zero -- market neutral" if abs(net["net_delta"]) < 0.1 else f"Delta {net['net_delta']:+.3f} -- directional exposure"
    theta_note = f"You earn {theta_net} per day from time decay"
    vega_note  = f"You {'lose' if net['net_vega'] < 0 else 'gain'} Rs.{abs(net['net_vega']):,.0f} if VIX moves 1 point"

    interp = (
        f"\n[dim]{delta_note}[/dim]\n"
        f"[dim]{theta_note}[/dim]\n"
        f"[dim]{vega_note}[/dim]"
    )

    from rich.console import Group
    content = Group(tbl, Text.from_markup(interp))
    return Panel(content, box=box.ROUNDED, title="[bold]Your Positions & Greeks[/bold]", padding=(0, 1))


# -----------------------------------------------------------------------
# Master render
# -----------------------------------------------------------------------
def render_all(data, remaining_secs):
    header   = make_header(data, remaining_secs)
    chain    = make_chain_table(data)
    analysis = make_oi_analysis(data)
    greeks   = make_greeks_panel(data)

    console.print(header)

    # Chain + analysis side by side
    console.print(Columns([chain, analysis], equal=False, expand=True))

    if greeks:
        console.print(greeks)

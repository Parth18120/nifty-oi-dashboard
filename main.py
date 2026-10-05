"""
main.py
Nifty OI and Greeks Dashboard
Run: python main.py
"""

import io
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if hasattr(sys.stdout, "buffer"):
    try:
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

from config import (
    ALERT_OI_BUILD_MIN,
    ALERT_PCR_HIGH,
    ALERT_PCR_LOW,
    MY_POSITIONS,
    REFRESH_SECONDS,
    STRIKES_TO_SHOW,
)
from rich.console import Console

from oi_dashboard.analytics import chain_analysis as A
from oi_dashboard.analytics import greeks as G
from oi_dashboard.feeds import dhan_feed as F
from oi_dashboard.terminal import display as D

IST     = timezone(timedelta(hours=5, minutes=30))
console = Console(force_terminal=True)


def is_market_open():
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return 9 * 60 + 15 <= t <= 15 * 60 + 30


def fetch_all():
    """Fetch everything needed for one dashboard refresh."""
    spot_data = F.fetch_nifty_spot()
    spot      = spot_data["ltp"]
    vix       = F.fetch_india_vix()
    chain     = F.fetch_option_chain(spot)
    expiries  = F.fetch_expiry_dates()
    dte       = expiries[0]["days_to_expiry"] if expiries else 3

    # If chain is empty (market closed / API error), return minimal data
    if not chain:
        return {
            "spot": spot_data, "vix": vix, "chain": [],
            "dte": dte, "pcr": 0.0,
            "pcr_label": "NO DATA", "pcr_color": "white",
            "max_pain": 0, "support": [], "resist": [],
            "oi_alerts": [], "iv_skew": {
                "ce_strike": 0, "ce_iv": 0.0,
                "pe_strike": 0, "pe_iv": 0.0,
                "skew": 0.0, "label": "No data", "color": "white",
            },
            "pos_greeks": None,
        }

    pcr              = A.compute_pcr(chain)
    pcr_lbl, pcr_col = A.pcr_label(pcr)
    max_pain         = A.compute_max_pain(chain)
    support, resist  = A.find_support_resistance(chain)
    oi_alerts        = A.compute_oi_buildup(chain)
    iv_skew          = A.compute_iv_skew(chain, spot)

    pos_greeks = None
    if MY_POSITIONS:
        pos_greeks = G.compute_position_greeks(MY_POSITIONS, spot, chain, dte)

    return {
        "spot":      spot_data,
        "vix":       vix,
        "chain":     chain,
        "dte":       dte,
        "pcr":       pcr,
        "pcr_label": pcr_lbl,
        "pcr_color": pcr_col,
        "max_pain":  max_pain,
        "support":   support,
        "resist":    resist,
        "oi_alerts": oi_alerts,
        "iv_skew":   iv_skew,
        "pos_greeks": pos_greeks,
    }


def check_alerts(data):
    if data["pcr"] >= ALERT_PCR_HIGH:
        console.print(f"[bold yellow][ALERT] PCR {data['pcr']:.2f} -- extreme fear, puts very overpriced[/bold yellow]")
    if 0 < data["pcr"] <= ALERT_PCR_LOW:
        console.print(f"[bold yellow][ALERT] PCR {data['pcr']:.2f} -- extreme greed, calls overpriced[/bold yellow]")
    for a in data["oi_alerts"][:3]:
        if abs(a["change"]) >= ALERT_OI_BUILD_MIN:
            console.print(f"[bold yellow][OI ALERT] {a['note']}[/bold yellow]")


def run():
    last_data     = [None]
    last_fetch    = [0.0]
    force_refresh = [False]
    quit_flag     = [False]

    def keyboard():
        try:
            import msvcrt
            while not quit_flag[0]:
                if msvcrt.kbhit():
                    ch = msvcrt.getch().upper()
                    if ch == b"Q":
                        quit_flag[0] = True
                    elif ch == b"R":
                        force_refresh[0] = True
                time.sleep(0.05)
        except ImportError:
            pass   # non-Windows fallback; no keyboard control

    threading.Thread(target=keyboard, daemon=True).start()

    def do_fetch():
        try:
            last_data[0]  = fetch_all()
            last_fetch[0] = time.time()
            check_alerts(last_data[0])
        except Exception as e:
            console.print(f"[red]Fetch error: {e}[/red]")
        force_refresh[0] = False

    if not is_market_open():
        console.print("[yellow]Market is closed. Fetching last available data...[/yellow]")

    do_fetch()

    while not quit_flag[0]:
        elapsed   = int(time.time() - last_fetch[0])
        remaining = max(0, REFRESH_SECONDS - elapsed)

        if last_data[0]:
            console.clear()
            if not is_market_open():
                console.print("[bold yellow]*** MARKET CLOSED -- displaying last available data ***[/bold yellow]")
            D.render_all(last_data[0], remaining)

        if remaining == 0 or force_refresh[0]:
            do_fetch()
        else:
            time.sleep(1)

    console.print("\n[cyan]Dashboard closed.[/cyan]")


if __name__ == "__main__":
    console.print("[bold cyan]Nifty OI Dashboard starting...[/bold cyan]")
    run()

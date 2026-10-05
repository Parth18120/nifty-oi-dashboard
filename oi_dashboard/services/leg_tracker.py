"""Background thread keeping open-trade leg prices fresh between full refreshes."""

from oi_dashboard.core import state


def refresh_open_trade_legs():
    """
    Refresh live leg prices for all OPEN trades.
    1. Reads chain snapshot for in-range strikes (no API cost).
    2. Batch-fetches out-of-range strikes (deep OTM hedges) via fetch_strike_prices.
    Called from the leg tracking thread every 10s.
    """
    import time as _t
    chain = (state.cache or {}).get("chain", [])
    if not chain:
        return
    cmap   = {r["strike"]: r for r in chain}
    now_ts = _t.time()

    # Collect all out-of-range (strike, side) pairs across all trades for batch fetch
    missing_by_expiry: dict = {}   # expiry_dt → set of (strike, side)

    for trade in state.paper_trades:
        if trade.get("status") != "OPEN":
            continue
        subs = trade.get("leg_subscription", [])
        if not subs:
            continue
        leg_prices = dict(trade.get("leg_prices") or {})
        expiry_dt  = trade.get("expiry_dt", "")

        for item in subs:
            strike, side = int(item[0]), str(item[1]).upper()
            row = cmap.get(strike)
            if row:
                ltp = float(row.get(f"{side.lower()}_ltp") or 0)
                bid = float(row.get(f"{side.lower()}_bid") or 0)
                ask = float(row.get(f"{side.lower()}_ask") or 0)
                leg_prices[f"{strike}_{side}"] = {"ltp": ltp, "bid": bid, "ask": ask, "src": "chain"}
            else:
                # Queue for batch fetch if we have an expiry
                if expiry_dt:
                    missing_by_expiry.setdefault(expiry_dt, set()).add((strike, side))
                else:
                    print(f"[LEG TRACK] #{trade['id']} {strike} {side} OTM and no expiry_dt — skipping")

        trade["leg_prices"]      = leg_prices
        trade["last_leg_update"] = now_ts

    # Batch-fetch all out-of-range strikes
    if missing_by_expiry:
        try:
            from oi_dashboard.feeds import dhan_feed as _F
            for expiry_dt, pairs in missing_by_expiry.items():
                fresh = _F.fetch_strike_prices(expiry_dt, list(pairs))
                for (strike, side), row in fresh.items():
                    # Write result into every trade that subscribes to this leg
                    for trade in state.paper_trades:
                        if trade.get("status") != "OPEN":
                            continue
                        subs2 = [(int(x[0]), str(x[1]).upper()) for x in (trade.get("leg_subscription") or [])]
                        if (int(strike), side) in subs2:
                            trade["leg_prices"][f"{strike}_{side}"] = {
                                "ltp": row.get("ltp", 0),
                                "bid": row.get("bid", 0),
                                "ask": row.get("ask", 0),
                                "src": "direct_fetch",
                            }
                            trade["last_leg_update"] = now_ts
                            print(f"[LEG TRACK] #{trade['id']} {strike} {side} fetched directly: "
                                  f"ltp={row.get('ltp')} bid={row.get('bid')} ask={row.get('ask')}")
        except Exception as _fe:
            print(f"[LEG TRACK] batch fetch error: {_fe}")


def leg_tracking_loop():
    """Daemon thread: refreshes open trade leg prices from chain cache every 10s."""
    import time as _t
    while True:
        try:
            refresh_open_trade_legs()
        except Exception as _e:
            print(f"[LEG TRACK] error: {_e}")
        _t.sleep(10)


def start_leg_tracking_thread():
    import threading
    t = threading.Thread(target=leg_tracking_loop, daemon=True, name="leg-tracker")
    t.start()
    print("[LEG TRACK] background thread started (10s refresh from chain cache)")

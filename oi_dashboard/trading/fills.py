"""Leg pricing and two-tick fill confirmation for paper fills."""

from datetime import datetime

from oi_dashboard.core import state
from oi_dashboard.core.constants import IST


def get_leg_price(row, side, action):
    """
    Returns a real market price for one option leg. NEVER returns a hardcoded default.

    side:   'ce' or 'pe'
    action: 'sell' (we are short) or 'buy' (we are long/hedge)

    Priority for sells: bid > mid > ltp*0.95
    Priority for buys:  ask > mid > ltp*1.05
    Returns None if the strike is genuinely untradeable (no bid/ask/ltp at all).
    """
    p = side.lower()
    ltp = float(row.get(f"{p}_ltp") or 0)
    bid = float(row.get(f"{p}_bid") or 0)
    ask = float(row.get(f"{p}_ask") or 0)

    if action == "sell":
        if bid > 0:
            return round(bid, 2)
        if bid > 0 and ask > 0:
            return round((bid + ask) / 2, 2)
        if ltp > 0:
            return round(ltp * 0.95, 2)
        return None

    if action == "buy":
        if ask > 0:
            return round(ask, 2)
        if bid > 0 and ask > 0:
            return round((bid + ask) / 2, 2)
        if ltp > 0:
            return round(ltp * 1.05, 2)
        return None

    return None


def fill_confirm(key, current_prices):
    """
    Two-tick fill confirmation for auto entries.

    First call (no pending): stores tick-1 prices + timestamp, returns (False, {}).
    Subsequent calls < 15s:  still waiting, returns (False, {}).
    Call >= 15s later:       compares current prices to tick-1.
      - Any leg moved > 15%: rejects, clears pending, returns (False, {}).
      - All legs OK:          returns (True, avg_prices) and clears pending.

    current_prices is a dict e.g. {"pe_sell": 28.5, "pe_buy": 0.50, ...}.
    """
    now = datetime.now(IST)

    pending = state.pending_entries.get(key)
    if pending is None:
        state.pending_entries[key] = {"tick1": current_prices.copy(), "time": now}
        print(f"[FILL] Pending {key} tick1={current_prices} — confirming in >=15s")
        return False, {}

    elapsed = (now - pending["time"]).total_seconds()
    if elapsed < 15:
        return False, {}

    tick1 = pending["tick1"]
    for leg, p2 in current_prices.items():
        p1 = tick1.get(leg, 0)
        if p1 > 0 and abs(p2 - p1) / p1 > 0.15:
            print(f"[FILL REJECTED] {key} leg={leg} ₹{p1:.2f}→₹{p2:.2f} ({abs(p2-p1)/p1*100:.0f}% move >15%)")
            del state.pending_entries[key]
            return False, {}

    avg = {leg: round((tick1.get(leg, p2) + p2) / 2, 2) for leg, p2 in current_prices.items()}
    del state.pending_entries[key]
    print(f"[FILL CONFIRMED] {key} avg={avg} elapsed={elapsed:.0f}s")
    return True, avg


def fetch_leg_or_chain(cmap, strike, side, action, expiry_dt):
    """
    Try cmap (chain snapshot) first. If strike missing from cmap (out-of-range),
    call fetch_strike_prices for a fresh targeted fetch.
    Returns price from get_leg_price, or None.
    """
    row = cmap.get(strike)
    if row:
        p = get_leg_price(row, side, action)
        if p is not None:
            return p
    # Strike not in chain snapshot (probably out of ATM±1000pt range) — fetch directly
    if expiry_dt:
        try:
            from oi_dashboard.feeds import dhan_feed as _F
            fresh = _F.fetch_strike_prices(expiry_dt, [(strike, side)])
            fresh_row = fresh.get((strike, side.upper()))
            if fresh_row:
                ltp = fresh_row.get("ltp", 0)
                bid = fresh_row.get("bid", 0)
                ask = fresh_row.get("ask", 0)
                synthetic_row = {f"{side.lower()}_ltp": ltp,
                                 f"{side.lower()}_bid": bid,
                                 f"{side.lower()}_ask": ask}
                return get_leg_price(synthetic_row, side, action)
        except Exception as _fe:
            print(f"[LEG FETCH] {strike} {side}: {_fe}")
    return None


def fetch_fresh_exit_prices(trade):
    """
    On exit: call fetch_strike_prices for all legs to get accurate bid/ask.
    Sell legs close at ask (buying back); buy legs close at bid (selling to close).
    Updates trade with {leg_key}_exit_price fields for display/audit.
    Non-fatal — if fetch fails, logs and continues with chain-snapshot prices.
    """
    expiry_dt = trade.get("expiry_dt", "")
    subs      = trade.get("leg_subscription", [])
    if not expiry_dt or not subs:
        return

    t_type = trade.get("type", "")
    pairs  = [(s, side) for s, side in subs]
    try:
        from oi_dashboard.feeds import dhan_feed as _F
        fresh = _F.fetch_strike_prices(expiry_dt, pairs)
        for (strike, side) in subs:
            key = f"{strike}_{side}"
            row = fresh.get((int(strike), side.upper()))
            if not row:
                print(f"[EXIT FETCH] #{trade['id']} no data for {strike} {side}")
                continue
            ltp = row.get("ltp", 0)
            bid = row.get("bid", 0)
            ask = row.get("ask", 0)
            # Determine whether this leg was sold or bought at entry
            entry_side = exit_action_for_leg(trade, strike, side)
            if entry_side == "sell":   # was sold → close by buying back → pay ask
                px = ask if ask > 0 else (ltp or 0)
            else:                       # was bought → close by selling → receive bid
                px = bid if bid > 0 else (ltp or 0)
            trade[f"{key}_exit_price"] = round(px, 2)
            print(f"[EXIT FETCH] #{trade['id']} {strike} {side} exit@{px:.2f} "
                  f"(bid={bid} ask={ask} ltp={ltp})")
    except Exception as _fe:
        print(f"[EXIT FETCH] #{trade['id']} fetch failed: {_fe}")


def exit_action_for_leg(trade, strike, side):
    """Return 'sell' if leg was sold at entry (now buying back), else 'buy'."""
    t = trade.get("type", "")
    if t == "IRON_CONDOR":
        sells = {trade.get("pe_sell_strike"), trade.get("ce_sell_strike")}
        return "sell" if strike in sells else "buy"
    if t in ("GAP_SPREAD", "STRANGLE"):
        return "sell" if strike == trade.get("sell_strike") else "buy"
    return "sell"

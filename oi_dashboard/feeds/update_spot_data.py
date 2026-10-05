"""
update_spot_data.py
Maintains spot_all.csv in the dashboard folder with last 90 trading days
of Nifty spot OHLC (date,open,high,low,close).

Run manually any time, or automatically by the server after market close.
"""

import csv
import datetime
import math
import os
from pathlib import Path

from oi_dashboard.core.paths import DATA_DIR


def _parse_date_safe(s):
    """Handles DD-MM-YYYY, YYYY-MM-DD, D/M/YYYY → date object. Returns None on failure."""
    s = str(s).strip()[:10]
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None

# ── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR       = DATA_DIR
OUTPUT_CSV     = BASE_DIR / "spot_all.csv"
OLD_SOURCE_CSV = Path(os.environ.get("OI_BACKFILL_CSV", "backfill/spot_all.csv"))

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# ── Indian market holidays 2025-2026 ─────────────────────────────────────────
MARKET_HOLIDAYS = {
    # 2025
    "2025-01-26", "2025-02-26", "2025-03-14", "2025-03-31",
    "2025-04-10", "2025-04-14", "2025-04-18", "2025-05-01",
    "2025-08-15", "2025-08-27", "2025-10-02", "2025-10-02",
    "2025-10-20", "2025-10-21", "2025-10-22", "2025-11-05",
    "2025-12-25",
    # 2026
    "2026-01-26",  # Republic Day
    "2026-03-02",  # Holi
    "2026-03-30",  # Ram Navami
    "2026-04-02",  # Mahavir Jayanti
    "2026-04-03",  # Good Friday
    "2026-04-14",  # Dr. Ambedkar Jayanti / Baisakhi
    "2026-05-01",  # Maharashtra Day
    "2026-08-15",  # Independence Day
    "2026-10-02",  # Gandhi Jayanti
    "2026-11-04",  # Diwali Laxmi Puja
    "2026-11-05",  # Diwali Balipratipada
    "2026-11-23",  # Gurunanak Jayanti
    "2026-12-25",  # Christmas
}


def is_trading_day(d: datetime.date) -> bool:
    return d.weekday() < 5 and d.strftime("%Y-%m-%d") not in MARKET_HOLIDAYS


def trading_days_between(start: datetime.date, end: datetime.date) -> list:
    """Returns list of trading day date strings from start (exclusive) to end (inclusive)."""
    days = []
    cur = start + datetime.timedelta(days=1)
    while cur <= end:
        if is_trading_day(cur):
            days.append(cur.strftime("%Y-%m-%d"))
        cur += datetime.timedelta(days=1)
    return days


# ── Load existing CSV ─────────────────────────────────────────────────────────
def load_existing_csv(path: Path) -> dict:
    """Returns {YYYY-MM-DD: {open,high,low,close}} dict. Normalizes mixed date formats."""
    rows = {}
    if not path.exists():
        return rows
    bad = 0
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw = str(row.get("date", "")).strip()
            d = _parse_date_safe(raw)
            if d is None:
                bad += 1
                continue
            date_str = d.isoformat()  # always YYYY-MM-DD
            try:
                close = float(row.get("close", 0) or row.get("Close", 0) or 0)
                if close < 100:
                    continue
                open_p  = float(row.get("open",  close) or row.get("Open",  close) or close)
                high_p  = float(row.get("high",  close) or row.get("High",  close) or close)
                low_p   = float(row.get("low",   close) or row.get("Low",   close) or close)
                rows[date_str] = {"open": open_p, "high": high_p, "low": low_p, "close": close}
            except (ValueError, TypeError):
                continue
    if bad:
        print(f"[SpotUpdate] Skipped {bad} rows with unparseable dates")
    return rows


# ── Aggregate minute-level source CSV to daily OHLC ──────────────────────────
def aggregate_old_source(path: Path) -> dict:
    """
    Old spot_all.csv has datetime,close,open,high,low,volume at minute level.
    Aggregate to daily: first bar open, max high, min low, last bar close.
    """
    if not path.exists():
        return {}
    by_day = {}  # date_str -> list of (time_str, open, high, low, close)
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            dt_str = str(row.get("datetime", "") or row.get("date", "")).strip()
            if not dt_str:
                continue
            d = _parse_date_safe(dt_str[:10])
            if d is None:
                continue
            date_str = d.isoformat()
            try:
                close = float(row.get("close", 0) or 0)
                if close < 100:
                    continue
                open_p = float(row.get("open",  close) or close)
                high_p = float(row.get("high",  close) or close)
                low_p  = float(row.get("low",   close) or close)
                time_s = dt_str[11:16] if len(dt_str) > 10 else "09:15"
                by_day.setdefault(date_str, []).append((time_s, open_p, high_p, low_p, close))
            except (ValueError, TypeError):
                continue

    result = {}
    for date_str, bars in by_day.items():
        bars.sort(key=lambda x: x[0])
        day_open  = bars[0][1]
        day_high  = max(b[2] for b in bars)
        day_low   = min(b[3] for b in bars)
        day_close = bars[-1][4]
        result[date_str] = {"open": day_open, "high": day_high, "low": day_low, "close": day_close}
    return result


# ── Fetch daily OHLC from Dhan API (minute-level, then aggregate) ─────────────
def fetch_daily_ohlc_from_api(from_date: str, to_date: str) -> dict:
    """
    Fetches minute bars via intraday_minute_data and aggregates to daily OHLC.
    Returns {date_str: {open,high,low,close}}.
    """
    try:
        from config import ACCESS_TOKEN, CLIENT_ID
        from dhanhq import DhanContext
        from dhanhq import dhanhq as DhanHQ
        dhan = DhanHQ(DhanContext(CLIENT_ID, ACCESS_TOKEN))

        resp = dhan.intraday_minute_data(
            security_id="13",
            exchange_segment="IDX_I",
            instrument_type="INDEX",
            from_date=from_date,
            to_date=to_date,
            interval=1,
        )
        if not resp or resp.get("status") != "success":
            print(f"[SpotUpdate] API error: {resp.get('remarks', 'unknown')}")
            return {}

        d = resp.get("data", {})
        opens      = d.get("open",      [])
        highs      = d.get("high",      [])
        lows       = d.get("low",       [])
        closes     = d.get("close",     [])
        timestamps = d.get("timestamp", [])

        if not closes or not timestamps:
            return {}

        by_day = {}
        for i, ts in enumerate(timestamps):
            dt = datetime.datetime.fromtimestamp(float(ts), tz=IST)
            date_str = dt.strftime("%Y-%m-%d")
            time_str = dt.strftime("%H:%M")
            try:
                bar = (time_str,
                       float(opens[i]),
                       float(highs[i]),
                       float(lows[i]),
                       float(closes[i]))
                by_day.setdefault(date_str, []).append(bar)
            except (IndexError, TypeError, ValueError):
                continue

        result = {}
        for date_str, bars in by_day.items():
            bars.sort(key=lambda x: x[0])
            result[date_str] = {
                "open":  bars[0][1],
                "high":  max(b[2] for b in bars),
                "low":   min(b[3] for b in bars),
                "close": bars[-1][4],
            }
        return result

    except Exception as e:
        print(f"[SpotUpdate] API fetch failed: {e}")
        return {}


# ── Compute RV ────────────────────────────────────────────────────────────────
def compute_rv(prices: list, window: int) -> float | None:
    if len(prices) < window + 1:
        return None
    px = prices[-(window + 1):]
    rets = [math.log(px[i] / px[i - 1]) for i in range(1, len(px)) if px[i - 1] > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var  = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return round(math.sqrt(var * 252) * 100, 2)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    today     = datetime.date.today()
    today_str = today.strftime("%Y-%m-%d")
    now_ist   = datetime.datetime.now(IST)

    # ── Step 1: Load existing dashboard CSV ──────────────────────────────────
    existing = load_existing_csv(OUTPUT_CSV)
    if existing:
        dates_sorted = sorted(existing.keys())
        print(f"[SpotUpdate] Loaded {len(existing)} existing rows "
              f"({dates_sorted[0]} to {dates_sorted[-1]})")
    else:
        print("[SpotUpdate] No existing spot_all.csv — starting fresh")
        dates_sorted = []

    # ── Step 2: Seed from old source CSV (one-time backfill) ─────────────────
    old_data = aggregate_old_source(OLD_SOURCE_CSV)
    if old_data:
        before = len(existing)
        existing.update({k: v for k, v in old_data.items() if k not in existing})
        seeded = len(existing) - before
        if seeded:
            print(f"[SpotUpdate] Seeded {seeded} rows from old source CSV")

    # ── Step 3: Find missing trading days ─────────────────────────────────────
    last_date_str = max(existing.keys()) if existing else "2025-12-31"
    last_date     = datetime.date.fromisoformat(last_date_str)

    # Only fetch up to yesterday (today's close only available after 15:30)
    fetch_end = today
    if now_ist.hour < 15 or (now_ist.hour == 15 and now_ist.minute < 30):
        fetch_end = today - datetime.timedelta(days=1)

    missing_days = trading_days_between(last_date, fetch_end)
    # Filter out days we already have
    missing_days = [d for d in missing_days if d not in existing]

    if not missing_days:
        print("[SpotUpdate] Already up to date — nothing to fetch")
    else:
        print(f"[SpotUpdate] Fetching {len(missing_days)} missing days: "
              f"{missing_days[0]} … {missing_days[-1]}")

        # Fetch in one API call (from first missing to fetch_end)
        fetched = fetch_daily_ohlc_from_api(missing_days[0], fetch_end.strftime("%Y-%m-%d"))

        added    = 0
        skipped  = 0
        for day in missing_days:
            if day in fetched:
                existing[day] = fetched[day]
                added += 1
            else:
                if is_trading_day(datetime.date.fromisoformat(day)):
                    print(f"[SpotUpdate]   {day} — no data from API (holiday or data lag)")
                skipped += 1

        print(f"[SpotUpdate] Added {added} rows, {skipped} days had no data")

    # ── Step 4: Keep last 90 trading days, sort, save ────────────────────────
    all_dates = sorted(existing.keys())
    # Keep at most 90 rows (enough for RV60 + buffer)
    if len(all_dates) > 90:
        all_dates = all_dates[-90:]
        existing  = {d: existing[d] for d in all_dates}

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "open", "high", "low", "close"])
        for d in all_dates:
            row = existing[d]
            writer.writerow([d,
                              round(row["open"],  2),
                              round(row["high"],  2),
                              round(row["low"],   2),
                              round(row["close"], 2)])

    print(f"[SpotUpdate] Final: {len(all_dates)} rows "
          f"({all_dates[0]} to {all_dates[-1]})")
    print(f"[SpotUpdate] Saved to {OUTPUT_CSV}")

    # ── Step 5: Print RV estimates ────────────────────────────────────────────
    closes = [existing[d]["close"] for d in all_dates if existing[d]["close"] > 0]
    rv5  = compute_rv(closes, 5)
    rv20 = compute_rv(closes, 20)
    rv60 = compute_rv(closes, 60)
    parts = []
    if rv5:  parts.append(f"RV5={rv5}%")
    if rv20: parts.append(f"RV20={rv20}%")
    if rv60: parts.append(f"RV60={rv60}%")
    if parts:
        print(f"[SpotUpdate] {' | '.join(parts)}")


def update_sensex_csv():
    """Keep sensex_spot_all.csv current — mirrors main() for Sensex."""
    try:
        from oi_dashboard.feeds import sensex_spot_history as _sh
        _sh.main(days=120)
    except Exception as _e:
        print(f"[SpotUpdate] Sensex CSV update failed: {_e}")


if __name__ == "__main__":
    main()
    update_sensex_csv()

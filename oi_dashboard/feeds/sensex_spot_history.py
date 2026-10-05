"""
sensex_spot_history.py
Fetch Sensex daily OHLC and write to sensex_spot_all.csv.
Run once to seed, then run daily (or let update_spot_data call it).
Uses the same intraday_minute_data approach as update_spot_data.py.
"""
import csv
import datetime
from pathlib import Path

from oi_dashboard.core.paths import DATA_DIR

BASE_DIR    = DATA_DIR
OUTPUT_CSV  = BASE_DIR / "sensex_spot_all.csv"
SENSEX_ID   = "51"
IST         = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def fetch_sensex_daily_ohlc(from_date: str, to_date: str) -> dict:
    """
    Fetch Sensex minute bars and aggregate to daily OHLC.
    Returns {date_str: {open,high,low,close}}.
    """
    from config import ACCESS_TOKEN, CLIENT_ID
    from dhanhq import DhanContext
    from dhanhq import dhanhq as DhanHQ
    dhan = DhanHQ(DhanContext(CLIENT_ID, ACCESS_TOKEN))

    resp = dhan.intraday_minute_data(
        security_id=SENSEX_ID,
        exchange_segment="IDX_I",
        instrument_type="INDEX",
        from_date=from_date,
        to_date=to_date,
        interval=1,
    )
    if not resp or resp.get("status") != "success":
        print(f"[SENSEX OHLC] API error: {resp.get('remarks', resp) if resp else 'None'}")
        return {}

    d          = resp.get("data", {})
    opens      = d.get("open",      [])
    highs      = d.get("high",      [])
    lows       = d.get("low",       [])
    closes     = d.get("close",     [])
    timestamps = d.get("timestamp", [])

    if not closes or not timestamps:
        print("[SENSEX OHLC] Empty response")
        return {}

    by_day = {}
    for i, ts in enumerate(timestamps):
        dt       = datetime.datetime.fromtimestamp(float(ts), tz=IST)
        date_str = dt.strftime("%Y-%m-%d")
        time_str = dt.strftime("%H:%M")
        try:
            bar = (time_str, float(opens[i]), float(highs[i]),
                   float(lows[i]), float(closes[i]))
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


def load_existing_sensex_csv() -> dict:
    rows = {}
    if not OUTPUT_CSV.exists():
        return rows
    with open(OUTPUT_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                close = float(row["close"])
                if close < 1000:
                    continue
                rows[row["date"]] = {
                    "open":  float(row["open"]),
                    "high":  float(row["high"]),
                    "low":   float(row["low"]),
                    "close": close,
                }
            except (KeyError, ValueError):
                continue
    return rows


def save_sensex_csv(data: dict):
    if not data:
        print("[SENSEX OHLC] No data to save")
        return
    dates = sorted(data.keys())[-120:]
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "open", "high", "low", "close"])
        for d in dates:
            r = data[d]
            writer.writerow([d, round(r["open"], 2), round(r["high"], 2),
                             round(r["low"], 2), round(r["close"], 2)])
    print(f"[SENSEX OHLC] Saved {len(dates)} rows → {OUTPUT_CSV}")
    if dates:
        print(f"[SENSEX OHLC] Range: {dates[0]} → {dates[-1]}")


def _date_chunks(from_str: str, to_str: str, chunk_days=85):
    """Split a date range into ≤chunk_days chunks (API limit is 90)."""
    cur   = datetime.date.fromisoformat(from_str)
    end   = datetime.date.fromisoformat(to_str)
    while cur <= end:
        chunk_end = min(cur + datetime.timedelta(days=chunk_days), end)
        yield cur.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")
        cur = chunk_end + datetime.timedelta(days=1)


def main(days=120):
    import time
    today   = datetime.date.today()
    now_ist = datetime.datetime.now(IST)
    fetch_end = today if (now_ist.hour > 15 or (now_ist.hour == 15 and now_ist.minute >= 30)) \
                else today - datetime.timedelta(days=1)

    existing = load_existing_sensex_csv()
    if existing:
        last = max(existing.keys())
        print(f"[SENSEX OHLC] Existing: {len(existing)} rows, last: {last}")
        from_date = (datetime.date.fromisoformat(last) + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        from_date = (fetch_end - datetime.timedelta(days=int(days * 1.6))).strftime("%Y-%m-%d")
        print(f"[SENSEX OHLC] No existing CSV — seeding from {from_date}")

    to_date = fetch_end.strftime("%Y-%m-%d")
    if from_date > to_date:
        print("[SENSEX OHLC] Already up to date")
    else:
        total = 0
        for chunk_from, chunk_to in _date_chunks(from_date, to_date):
            print(f"[SENSEX OHLC] Fetching chunk {chunk_from} → {chunk_to}")
            fetched = fetch_sensex_daily_ohlc(chunk_from, chunk_to)
            existing.update(fetched)
            total += len(fetched)
            if fetched:
                time.sleep(1)   # brief pause between chunks
        print(f"[SENSEX OHLC] Total fetched: {total} trading days")

    save_sensex_csv(existing)


if __name__ == "__main__":
    main(days=120)

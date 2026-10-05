"""
fetch_nse_bhavcopy.py — Download NSE F&O bhavcopy files and extract
NIFTY option chain data into option_eod_history SQLite table.

Usage:
  python fetch_nse_bhavcopy.py --days 90
  python fetch_nse_bhavcopy.py --from 2024-01-01 --to 2024-12-31
  python fetch_nse_bhavcopy.py --check
"""
import argparse
import csv
import io
import os
import sys
import time
import urllib.request
import zipfile
from datetime import date, datetime, timedelta

from oi_dashboard.storage import db

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://www.nseindia.com",
}

_PAUSE = 2.0   # seconds between requests


def try_download_bhavcopy(dt: date) -> bytes | None:
    """
    Try multiple URL formats for NSE F&O bhavcopy.
    Returns raw zip bytes or None if not available.
    """
    month_abbr = dt.strftime("%b").upper()
    year       = dt.strftime("%Y")
    dd         = dt.strftime("%d")
    yyyymmdd   = dt.strftime("%Y%m%d")

    urls = [
        # Newer format (post ~2023)
        f"https://nsearchives.nseindia.com/content/fo/"
        f"BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip",
        # Older format
        f"https://www.nseindia.com/content/historical/"
        f"DERIVATIVES/{year}/{month_abbr}/"
        f"fo{dd}{month_abbr}{year}bhav.csv.zip",
        # Alternative archive path
        f"https://nsearchives.nseindia.com/archives/fo/bhav/"
        f"fo{dd}{month_abbr}{year}bhav.csv.zip",
    ]

    for url in urls:
        try:
            req = urllib.request.Request(url, headers=NSE_HEADERS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 200:
                    data = resp.read()
                    if len(data) > 500:   # sanity: non-empty zip
                        return data
        except Exception:
            continue
    return None


def parse_bhavcopy(zip_bytes: bytes, underlying: str = "NIFTY") -> list:
    """
    Parse bhavcopy zip for the given underlying (NIFTY options only).

    Supports two formats:
    - New (2023+): BhavCopy_NSE_FO_..._F_0000.csv
      Cols: TradDt, TckrSymb, XpryDt, StrkPric, OptnTp,
            OpnPric, HghPric, LwPric, ClsPric, SttlmPric,
            TtlTradgVol, OpnIntrst
    - Old: fo{dd}{mon}{yyyy}bhav.csv
      Cols: TIMESTAMP, SYMBOL, EXPIRY_DT, STRIKE_PR, OPTION_TYP,
            OPEN_PRICE, HIGH_PRICE, LOW_PRICE, CLOSE_PRICE, SETTLE_PR,
            CONTRACTS, OPEN_INT
    """
    rows = []
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            csv_name = next((n for n in names if n.lower().endswith(".csv")), None)
            if not csv_name:
                return rows
            with zf.open(csv_name) as f:
                text = f.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  [parse] zip error: {e}")
        return rows

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return rows

    # Detect format by checking for new-format column
    fieldnames_stripped = [h.strip() for h in reader.fieldnames]
    is_new_format = "TckrSymb" in fieldnames_stripped

    for raw in reader:
        row = {k.strip(): (v.strip() if v else "") for k, v in raw.items()}

        if is_new_format:
            # New format: TckrSymb = NIFTY, OptnTp = CE/PE
            sym      = row.get("TckrSymb", "")
            opt_type = row.get("OptnTp", "").upper()
            raw_exp  = row.get("XpryDt", "")
            raw_date = row.get("TradDt", "")
            try:
                strike  = float(row.get("StrkPric", 0) or 0)
                open_p  = _f(row.get("OpnPric"))
                high_p  = _f(row.get("HghPric"))
                low_p   = _f(row.get("LwPric"))
                close_p = _f(row.get("ClsPric"))
                settle  = _f(row.get("SttlmPric") or close_p)
                volume  = int(float(row.get("TtlTradgVol", 0) or 0))
                oi      = int(float(row.get("OpnIntrst", 0) or 0))
            except (ValueError, TypeError):
                continue
        else:
            # Old format
            sym      = row.get("SYMBOL", row.get("UNDERLYING", ""))
            instr    = row.get("INSTRUMENT", row.get("INSTTYPE", ""))
            if "OPTIDX" not in instr.upper():
                continue
            opt_type = row.get("OPTION_TYP", row.get("OPTTYPE", "")).upper()
            raw_exp  = row.get("EXPIRY_DT", row.get("EXPIRY", ""))
            raw_date = row.get("TIMESTAMP", row.get("DATE1", row.get("TRADE_DATE", "")))
            try:
                strike  = float(row.get("STRIKE_PR", row.get("STRIKEPRICE", 0)) or 0)
                open_p  = _f(row.get("OPEN_PRICE",  row.get("OPEN",  0)))
                high_p  = _f(row.get("HIGH_PRICE",  row.get("HIGH",  0)))
                low_p   = _f(row.get("LOW_PRICE",   row.get("LOW",   0)))
                close_p = _f(row.get("CLOSE_PRICE", row.get("CLOSE", 0)))
                settle  = _f(row.get("SETTLE_PR",   row.get("SETTLE", close_p)))
                volume  = int(float(row.get("CONTRACTS", row.get("VOLUME", 0)) or 0))
                oi      = int(float(row.get("OPEN_INT",  row.get("OI",     0)) or 0))
            except (ValueError, TypeError):
                continue

        if sym.upper() != underlying.upper():
            continue
        if opt_type not in ("CE", "PE"):
            continue

        expiry   = _parse_date(raw_exp)
        trade_dt = _parse_date(raw_date)
        if not expiry or not trade_dt:
            continue
        if strike <= 0:
            continue
        # Allow zero close (some far-OTM options settle at 0)

        rows.append({
            "date":       trade_dt,
            "expiry":     expiry,
            "strike":     strike,
            "opt_type":   opt_type,
            "open":       open_p,
            "high":       high_p,
            "low":        low_p,
            "close":      close_p,
            "settle":     settle,
            "volume":     volume,
            "oi":         oi,
            "underlying": underlying,
        })

    return rows


def _f(v) -> float:
    try:
        return float(v or 0)
    except (ValueError, TypeError):
        return 0.0


def _parse_date(s: str) -> str | None:
    """Parse various date formats → YYYY-MM-DD string."""
    s = str(s).strip()[:12]
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y",
                "%d%b%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _dates_already_in_db(underlying: str = "NIFTY") -> set:
    with db._conn() as c:
        rows = c.execute(
            "SELECT DISTINCT date FROM option_eod_history WHERE underlying=?",
            (underlying,)
        )
        return {r[0] for r in rows}


def run_download(from_date: date, to_date: date, underlying: str = "NIFTY"):
    """
    Download bhavcopy for each trading day in [from_date, to_date].
    Skips weekends and dates already in DB.
    """
    db.init_db()
    already = _dates_already_in_db(underlying)
    total_rows = 0
    days_ok    = 0
    days_skip  = 0
    days_fail  = 0

    dt = from_date
    while dt <= to_date:
        # Skip weekends
        if dt.weekday() >= 5:
            dt += timedelta(days=1)
            continue

        date_str = dt.isoformat()
        if date_str in already:
            print(f"  {date_str}: already in DB — skip")
            dt += timedelta(days=1)
            days_skip += 1
            continue

        print(f"  {date_str}: downloading...", end="", flush=True)
        raw = try_download_bhavcopy(dt)
        if raw is None:
            print(" NOT FOUND (holiday or format mismatch)")
            days_fail += 1
            dt += timedelta(days=1)
            time.sleep(_PAUSE)
            continue

        rows = parse_bhavcopy(raw, underlying)
        if not rows:
            print(f" downloaded but 0 {underlying} OPTIDX rows parsed")
            days_fail += 1
            dt += timedelta(days=1)
            time.sleep(_PAUSE)
            continue

        inserted = db.insert_eod_rows(rows)
        print(f" {len(rows)} rows parsed, {inserted} inserted")
        total_rows += inserted
        days_ok    += 1
        dt += timedelta(days=1)
        time.sleep(_PAUSE)

    print(f"\nDone. {days_ok} days downloaded, {days_skip} skipped (already in DB), "
          f"{days_fail} not found.")
    print(f"Total rows inserted: {total_rows:,}")
    return total_rows


def run_check():
    db.init_db()
    stats = db.get_eod_dates_available("NIFTY")
    if not stats.get("n_days"):
        print("option_eod_history: empty — no data downloaded yet.")
        print("Run: python fetch_nse_bhavcopy.py --days 90")
        return
    print(f"option_eod_history (NIFTY OPTIDX):")
    print(f"  Date range : {stats['min_date']} to {stats['max_date']}")
    print(f"  Trading days: {stats['n_days']}")
    print(f"  Total rows  : {stats['n_rows']:,}")
    print(f"  Avg rows/day: {stats['n_rows'] // max(stats['n_days'], 1):,}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download NSE F&O bhavcopy into SQLite")
    parser.add_argument("--days",  type=int,  default=None, help="Download last N calendar days")
    parser.add_argument("--from",  dest="from_date", default=None, help="Start date YYYY-MM-DD")
    parser.add_argument("--to",    dest="to_date",   default=None, help="End date YYYY-MM-DD")
    parser.add_argument("--check", action="store_true",             help="Show DB summary only")
    parser.add_argument("--underlying", default="NIFTY",            help="Underlying symbol (default NIFTY)")
    args = parser.parse_args()

    if args.check:
        run_check()
        sys.exit(0)

    today = date.today()
    if args.days:
        from_dt = today - timedelta(days=args.days)
        to_dt   = today - timedelta(days=1)   # yesterday (today's not published yet)
    elif args.from_date and args.to_date:
        from_dt = datetime.strptime(args.from_date, "%Y-%m-%d").date()
        to_dt   = datetime.strptime(args.to_date,   "%Y-%m-%d").date()
    else:
        parser.print_help()
        sys.exit(1)

    print(f"Downloading NSE F&O bhavcopy for {args.underlying}: {from_dt} to {to_dt}")
    print(f"Pausing {_PAUSE}s between requests to respect NSE rate limits.\n")
    run_download(from_dt, to_dt, args.underlying)

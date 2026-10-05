"""
db.py
SQLite persistence layer for the Nifty OI Dashboard.
All tables use IF NOT EXISTS — safe to call init_db() on every startup.
Schema changes via ALTER TABLE only — existing data is never destroyed.
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from oi_dashboard.core.paths import ROOT

IST = timezone(timedelta(hours=5, minutes=30))

try:
    from config import DB_PATH
except ImportError:
    DB_PATH = "data/dashboard.db"

_DB_FILE = os.path.join(str(ROOT), DB_PATH)


def _conn():
    os.makedirs(os.path.dirname(_DB_FILE), exist_ok=True)
    c = sqlite3.connect(_DB_FILE, timeout=10, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init_db():
    """Create all tables and run any pending ALTER TABLE migrations."""
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS spot_daily (
            date  TEXT PRIMARY KEY,
            open  REAL,
            high  REAL,
            low   REAL,
            close REAL
        );

        CREATE TABLE IF NOT EXISTS paper_trades (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date            TEXT,
            strike                INTEGER,
            opt_type              TEXT,
            entry_time            TEXT,
            entry_ltp             REAL,
            entry_fair            REAL,
            entry_ovr             REAL,
            entry_spot            REAL,
            sigma_used            REAL,
            lots                  INTEGER,
            stop_price            REAL,
            target_price          REAL,
            exit_time             TEXT,
            exit_ltp              REAL,
            exit_reason           TEXT,
            gross_pnl             REAL,
            costs                 REAL,
            net_pnl               REAL,
            max_ltp               REAL,
            min_ltp               REAL,
            status                TEXT DEFAULT 'OPEN',
            vix_at_entry          REAL,
            regime_score_at_entry REAL,
            iv_at_entry           REAL,
            iv_at_exit            REAL,
            spot_at_exit          REAL,
            theta_pnl             REAL,
            delta_pnl             REAL,
            vega_pnl              REAL,
            residual_pnl          REAL,
            days_held             REAL,
            mode                  TEXT,
            position_type         TEXT DEFAULT 'SHORT_NAKED',
            trade_type            TEXT,
            net_credit_entry      REAL,
            pnl_total             REAL,
            pnl_net               REAL,
            costs_total           REAL,
            lot_size_used         INTEGER,
            legs                  TEXT,
            entry_reason          TEXT,
            exit_note             TEXT
        );

        CREATE TABLE IF NOT EXISTS surface_snapshots (
            ts            TEXT PRIMARY KEY,
            date          TEXT,
            spot          REAL,
            atm_iv        REAL,
            skew_rr25     REAL,
            skew_bf25     REAL,
            skew_slope    REAL,
            call_wing_iv  REAL,
            put_wing_iv   REAL
        );
        CREATE INDEX IF NOT EXISTS idx_surf_date ON surface_snapshots(date);

        CREATE TABLE IF NOT EXISTS option_chain_history (
            ts          TEXT NOT NULL,
            date        TEXT NOT NULL,
            expiry      TEXT NOT NULL,
            strike      INTEGER NOT NULL,
            opt_type    TEXT NOT NULL,
            ltp         REAL,
            iv          REAL,
            oi          INTEGER,
            oi_chg      INTEGER,
            bid         REAL,
            ask         REAL,
            delta       REAL,
            volume      INTEGER,
            spot        REAL,
            security_id TEXT,
            PRIMARY KEY (ts, expiry, strike, opt_type)
        );
        CREATE INDEX IF NOT EXISTS idx_och_date
            ON option_chain_history(date, expiry);
        CREATE INDEX IF NOT EXISTS idx_och_strike
            ON option_chain_history(date, strike, opt_type);

        CREATE TABLE IF NOT EXISTS option_eod_history (
            date        TEXT NOT NULL,
            expiry      TEXT NOT NULL,
            strike      REAL NOT NULL,
            opt_type    TEXT NOT NULL,
            open        REAL,
            high        REAL,
            low         REAL,
            close       REAL,
            settle      REAL,
            volume      INTEGER,
            oi          INTEGER,
            underlying  TEXT DEFAULT 'NIFTY',
            PRIMARY KEY (date, expiry, strike, opt_type)
        );
        CREATE INDEX IF NOT EXISTS idx_oeod_date
            ON option_eod_history(date, underlying);

        CREATE TABLE IF NOT EXISTS skew_history (
            date       TEXT NOT NULL,
            moneyness  INTEGER NOT NULL,
            opt_type   TEXT NOT NULL,
            scaled_iv  REAL NOT NULL,
            atm_iv     REAL NOT NULL,
            spot       REAL NOT NULL,
            PRIMARY KEY (date, moneyness, opt_type)
        );

        CREATE TABLE IF NOT EXISTS iv_snapshots (
            date   TEXT,
            time   TEXT,
            atm_iv REAL,
            spot   REAL,
            vix    REAL,
            PRIMARY KEY (date, time)
        );
        """)

    _run_migrations()
    _prune_option_chain_history(keep_days=90)
    print(f"[DB] Initialized: {_DB_FILE}")


def _run_migrations():
    """Add any columns that were introduced after initial schema creation."""
    with _conn() as c:
        existing = {row[1] for row in c.execute("PRAGMA table_info(paper_trades)")}
        new_cols = [
            ("vix_at_entry",          "REAL"),
            ("regime_score_at_entry", "REAL"),
            ("iv_at_entry",           "REAL"),
            ("iv_at_exit",            "REAL"),
            ("spot_at_exit",          "REAL"),
            ("theta_pnl",             "REAL"),
            ("delta_pnl",             "REAL"),
            ("vega_pnl",              "REAL"),
            ("residual_pnl",          "REAL"),
            ("days_held",             "REAL"),
            ("mode",                  "TEXT"),
            ("position_type",         "TEXT DEFAULT 'SHORT_NAKED'"),
            ("trade_type",            "TEXT"),
            ("net_credit_entry",      "REAL"),
            ("pnl_total",             "REAL"),
            ("pnl_net",               "REAL"),
            ("costs_total",           "REAL"),
            ("lot_size_used",         "INTEGER"),
            ("legs",                  "TEXT"),
            ("entry_reason",          "TEXT"),
            ("exit_note",             "TEXT"),
        ]
        for col, col_type in new_cols:
            if col not in existing:
                c.execute(f"ALTER TABLE paper_trades ADD COLUMN {col} {col_type}")
                print(f"[DB] Migration: added column paper_trades.{col}")


# ── paper_trades CRUD ──────────────────────────────────────────────────────────

def insert_trade(trade: dict) -> int:
    """Insert a new paper trade. Returns the new row id."""
    cols = [
        "trade_date", "strike", "opt_type", "entry_time",
        "entry_ltp", "entry_fair", "entry_ovr", "entry_spot", "sigma_used",
        "lots", "stop_price", "target_price", "status",
        "vix_at_entry", "iv_at_entry", "mode", "position_type",
        "trade_type", "net_credit_entry", "pnl_total", "lot_size_used",
        "legs", "entry_reason",
    ]
    vals = {c: _serialize(trade.get(c)) for c in cols}
    placeholders = ", ".join(f":{c}" for c in cols)
    col_list     = ", ".join(cols)
    with _conn() as c:
        cur = c.execute(
            f"INSERT INTO paper_trades ({col_list}) VALUES ({placeholders})", vals
        )
        return cur.lastrowid


def update_trade(trade_id: int, fields: dict):
    """Update arbitrary fields on a trade by id."""
    if not fields:
        return
    set_clause = ", ".join(f"{k} = :{k}" for k in fields)
    params = {k: _serialize(v) for k, v in fields.items()}
    params["id"] = trade_id
    with _conn() as c:
        c.execute(f"UPDATE paper_trades SET {set_clause} WHERE id = :id", params)


def get_open_trades() -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM paper_trades WHERE status = 'OPEN' ORDER BY entry_time"
        )]


def get_trades(date: str = None, limit: int = 200) -> list[dict]:
    with _conn() as c:
        if date:
            rows = c.execute(
                "SELECT * FROM paper_trades WHERE trade_date = ? ORDER BY entry_time", (date,)
            )
        else:
            rows = c.execute(
                "SELECT * FROM paper_trades ORDER BY entry_time DESC LIMIT ?", (limit,)
            )
        result = []
        for r in rows:
            d = dict(r)
            if d.get("legs") and isinstance(d["legs"], str):
                try:
                    d["legs"] = json.loads(d["legs"])
                except Exception:
                    pass
            result.append(d)
        return result


def get_closed_trades(limit: int = 500) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM paper_trades WHERE status != 'OPEN' ORDER BY exit_time DESC LIMIT ?",
            (limit,)
        )
        return [dict(r) for r in rows]


# ── JSON migration ────────────────────────────────────────────────────────────

def migrate_json_to_db(json_path: str):
    """
    One-time migration: load paper_trades_session.json into SQLite.
    Idempotent — skips trades whose (trade_date, entry_time, strike, opt_type) already exist.
    """
    if not os.path.exists(json_path):
        print(f"[DB] JSON file not found — nothing to migrate: {json_path}")
        return 0

    try:
        with open(json_path) as f:
            saved = json.load(f)
    except Exception as e:
        print(f"[DB] Could not read JSON: {e}")
        return 0

    trades = saved.get("trades", [])
    if not trades:
        print("[DB] JSON has no trades — migration skipped")
        return 0

    migrated = 0
    with _conn() as c:
        for t in trades:
            # Check for duplicate
            exists = c.execute(
                "SELECT id FROM paper_trades WHERE trade_date=? AND entry_time=? AND strike=? AND opt_type=?",
                (t.get("trade_date", ""), t.get("entry_time", ""),
                 t.get("strike", 0),      t.get("opt_type", ""))
            ).fetchone()
            if exists:
                continue

            legs_json = json.dumps(t.get("legs")) if t.get("legs") else None
            c.execute("""
                INSERT INTO paper_trades (
                    trade_date, strike, opt_type, entry_time,
                    entry_ltp, entry_fair, entry_ovr, entry_spot,
                    lots, stop_price, target_price, status,
                    exit_time, exit_ltp, exit_reason,
                    pnl_total, pnl_net, costs_total,
                    trade_type, net_credit_entry, lot_size_used, legs, entry_reason
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                t.get("trade_date"), t.get("strike"), t.get("opt_type"), t.get("entry_time"),
                t.get("entry_ltp"), t.get("entry_fair"), t.get("entry_ovr"), t.get("entry_spot"),
                t.get("lots"), t.get("stop_price"), t.get("target_price"),
                t.get("status", "OPEN"),
                t.get("exit_time"), t.get("exit_ltp"), t.get("exit_reason"),
                t.get("pnl_total"), t.get("pnl_net"), t.get("costs_total"),
                t.get("trade_type"), t.get("net_credit_entry"), t.get("lot_size_used"),
                legs_json, t.get("entry_reason"),
            ))
            migrated += 1

    print(f"[DB] Migrated {migrated} trades from JSON to SQLite")
    return migrated


# ── surface_snapshots ──────────────────────────────────────────────────────────

def log_surface_snapshot(snap: dict):
    with _conn() as c:
        c.execute("""
            INSERT OR REPLACE INTO surface_snapshots
            (ts, date, spot, atm_iv, skew_rr25, skew_bf25, skew_slope, call_wing_iv, put_wing_iv)
            VALUES (:ts,:date,:spot,:atm_iv,:skew_rr25,:skew_bf25,:skew_slope,:call_wing_iv,:put_wing_iv)
        """, snap)


def prune_surface_snapshots(keep_days: int = 30):
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=keep_days)).isoformat()
    with _conn() as c:
        c.execute("DELETE FROM surface_snapshots WHERE date < ?", (cutoff,))


# ── skew_history ───────────────────────────────────────────────────────────────

def log_skew_row(date: str, moneyness: int, opt_type: str,
                 scaled_iv: float, atm_iv: float, spot: float):
    with _conn() as c:
        c.execute("""
            INSERT OR REPLACE INTO skew_history (date, moneyness, opt_type, scaled_iv, atm_iv, spot)
            VALUES (?,?,?,?,?,?)
        """, (date, moneyness, opt_type, scaled_iv, atm_iv, spot))


def get_skew_history(moneyness: int, opt_type: str, days: int = 60) -> list[dict]:
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with _conn() as c:
        rows = c.execute("""
            SELECT * FROM skew_history
            WHERE moneyness=? AND opt_type=? AND date >= ?
            ORDER BY date
        """, (moneyness, opt_type, cutoff))
        return [dict(r) for r in rows]


# ── iv_snapshots ───────────────────────────────────────────────────────────────

def log_iv_snapshot(date: str, time: str, atm_iv: float, spot: float, vix: float):
    with _conn() as c:
        c.execute("""
            INSERT OR REPLACE INTO iv_snapshots (date, time, atm_iv, spot, vix)
            VALUES (?,?,?,?,?)
        """, (date, time, atm_iv, spot, vix))


def get_iv_snapshots(days: int = 90) -> list[dict]:
    """
    Return one row per trading day: {date, atm_iv, spot}.
    Primary source: iv_snapshots (daily avg).
    Fallback: surface_snapshots when iv_snapshots has < 5 distinct dates.
    """
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with _conn() as c:
        rows = c.execute("""
            SELECT date, AVG(atm_iv) AS atm_iv, AVG(spot) AS spot
            FROM iv_snapshots
            WHERE date >= ? AND atm_iv > 0
            GROUP BY date
            ORDER BY date
        """, (cutoff,)).fetchall()
        if len(rows) >= 5:
            return [dict(r) for r in rows]
        # Fallback: surface_snapshots (already populated by surface_engine)
        rows = c.execute("""
            SELECT date, AVG(atm_iv) AS atm_iv, AVG(spot) AS spot
            FROM surface_snapshots
            WHERE date >= ? AND atm_iv > 0
            GROUP BY date
            ORDER BY date
        """, (cutoff,)).fetchall()
        return [dict(r) for r in rows]


# ── spot_daily ─────────────────────────────────────────────────────────────────

def upsert_spot_daily(date: str, open_: float, high: float, low: float, close: float):
    with _conn() as c:
        c.execute("""
            INSERT OR REPLACE INTO spot_daily (date, open, high, low, close)
            VALUES (?,?,?,?,?)
        """, (date, open_, high, low, close))


def get_spot_daily(days: int = 65) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM spot_daily ORDER BY date DESC LIMIT ?", (days,)
        )
        return list(reversed([dict(r) for r in rows]))


def seed_spot_daily_from_csv(csv_path: str):
    """Seed spot_daily from an OHLC CSV file. Skips rows already in DB."""
    import csv as csv_mod
    if not os.path.exists(csv_path):
        return
    from datetime import date as _date

    def _parse_date(s):
        s = str(s).strip()[:10]
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(s, fmt).date().isoformat()
            except ValueError:
                continue
        return None

    inserted = 0
    with _conn() as c:
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv_mod.DictReader(f):
                d = _parse_date(row.get("date", "") or row.get("datetime", ""))
                if not d:
                    continue
                try:
                    close = float(row.get("close", 0) or 0)
                    if close < 100:
                        continue
                    exists = c.execute("SELECT 1 FROM spot_daily WHERE date=?", (d,)).fetchone()
                    if exists:
                        continue
                    c.execute(
                        "INSERT INTO spot_daily (date,open,high,low,close) VALUES (?,?,?,?,?)",
                        (d,
                         float(row.get("open", close) or close),
                         float(row.get("high", close) or close),
                         float(row.get("low",  close) or close),
                         close)
                    )
                    inserted += 1
                except Exception:
                    continue
    if inserted:
        print(f"[DB] Seeded {inserted} rows into spot_daily from {csv_path}")


# ── analytics_engine helpers ───────────────────────────────────────────────────

def get_paper_trades_closed(days: int = 90) -> list:
    """All closed trades from last N days, sorted by entry_time."""
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with _conn() as c:
        rows = c.execute("""
            SELECT * FROM paper_trades
            WHERE status != 'OPEN' AND trade_date >= ?
            ORDER BY entry_time
        """, (cutoff,))
        result = []
        for r in rows:
            d = dict(r)
            if d.get("legs") and isinstance(d["legs"], str):
                try:
                    d["legs"] = json.loads(d["legs"])
                except Exception:
                    pass
            result.append(d)
        return result


def get_surface_snapshots(days: int = 30) -> list:
    """Surface snapshots for last N days (for VIX proxy in regime analysis)."""
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with _conn() as c:
        rows = c.execute("""
            SELECT * FROM surface_snapshots
            WHERE date >= ?
            ORDER BY ts
        """, (cutoff,))
        return [dict(r) for r in rows]


def update_paper_trade_attribution(trade_id: int, **kwargs) -> None:
    """Update attribution columns on a closed trade by id."""
    if not kwargs:
        return
    allowed = {
        "theta_pnl", "delta_pnl", "vega_pnl", "residual_pnl",
        "days_held", "iv_at_exit", "spot_at_exit",
    }
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    set_clause = ", ".join(f"{k} = :{k}" for k in fields)
    params = {k: v for k, v in fields.items()}
    params["id"] = trade_id
    with _conn() as c:
        c.execute(f"UPDATE paper_trades SET {set_clause} WHERE id = :id", params)


# ── option_chain_history ───────────────────────────────────────────────────────

def _prune_option_chain_history(keep_days: int = 90):
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=keep_days)).isoformat()
    with _conn() as c:
        c.execute("DELETE FROM option_chain_history WHERE date < ?", (cutoff,))


def log_option_chain_snapshot(chain: list, spot: float, expiry: str, ts: str = None):
    """
    Log one snapshot of the full option chain to option_chain_history.
    Only logs during market hours (9:15–15:30 IST).
    Only logs if spot > 0 and len(chain) >= 5.
    """
    if not chain or len(chain) < 5 or spot <= 0:
        return

    now_ist = datetime.now(IST)
    h, m = now_ist.hour, now_ist.minute
    if not ((9 * 60 + 15) <= (h * 60 + m) <= (15 * 60 + 30)):
        return

    if ts is None:
        ts = now_ist.strftime("%Y-%m-%dT%H:%M:%S")
    date_str = ts[:10]

    rows = []
    for row in chain:
        strike = int(row.get("strike", 0))
        if not strike:
            continue
        for opt_type, pfx in (("CE", "ce_"), ("PE", "pe_")):
            rows.append((
                ts, date_str, expiry, strike, opt_type,
                row.get(pfx + "ltp"),
                row.get(pfx + "iv"),
                row.get(pfx + "oi"),
                row.get(pfx + "oi_chg"),
                row.get(pfx + "bid"),
                row.get(pfx + "ask"),
                None,  # delta not in chain snapshot
                row.get(pfx + "volume"),
                spot,
                str(row.get("security_id", "")) or None,
            ))

    if not rows:
        return

    with _conn() as c:
        c.executemany("""
            INSERT OR IGNORE INTO option_chain_history
            (ts, date, expiry, strike, opt_type, ltp, iv, oi, oi_chg,
             bid, ask, delta, volume, spot, security_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, rows)


def get_option_chain_history(date_str: str, strike: int, opt_type: str) -> list:
    with _conn() as c:
        rows = c.execute("""
            SELECT ts, ltp, iv, oi, oi_chg, bid, ask, volume, spot
            FROM option_chain_history
            WHERE date=? AND strike=? AND opt_type=?
            ORDER BY ts
        """, (date_str, strike, opt_type.upper()))
        return [dict(r) for r in rows]


# ── option_eod_history ─────────────────────────────────────────────────────────

def insert_eod_rows(rows: list) -> int:
    """Insert bhavcopy rows into option_eod_history. Returns count inserted."""
    if not rows:
        return 0
    inserted = 0
    with _conn() as c:
        for r in rows:
            try:
                c.execute("""
                    INSERT OR IGNORE INTO option_eod_history
                    (date, expiry, strike, opt_type, open, high, low, close,
                     settle, volume, oi, underlying)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    r.get("date"), r.get("expiry"), r.get("strike"), r.get("opt_type"),
                    r.get("open"), r.get("high"), r.get("low"), r.get("close"),
                    r.get("settle"), r.get("volume"), r.get("oi"),
                    r.get("underlying", "NIFTY"),
                ))
                inserted += 1
            except Exception:
                continue
    return inserted


def get_option_eod_history(date_str: str = None, underlying: str = "NIFTY") -> list:
    with _conn() as c:
        if date_str:
            rows = c.execute("""
                SELECT * FROM option_eod_history
                WHERE date=? AND underlying=?
                ORDER BY expiry, strike, opt_type
            """, (date_str, underlying))
        else:
            rows = c.execute("""
                SELECT * FROM option_eod_history
                WHERE underlying=?
                ORDER BY date, expiry, strike, opt_type
            """, (underlying,))
        return [dict(r) for r in rows]


def get_eod_dates_available(underlying: str = "NIFTY") -> dict:
    with _conn() as c:
        row = c.execute("""
            SELECT MIN(date) as min_date, MAX(date) as max_date, COUNT(DISTINCT date) as n_days,
                   COUNT(*) as n_rows
            FROM option_eod_history WHERE underlying=?
        """, (underlying,)).fetchone()
        return dict(row) if row else {}


def get_eod_strike_price(date_str: str, expiry: str, strike: float,
                         opt_type: str, underlying: str = "NIFTY") -> dict | None:
    with _conn() as c:
        row = c.execute("""
            SELECT * FROM option_eod_history
            WHERE date=? AND expiry=? AND strike=? AND opt_type=? AND underlying=?
        """, (date_str, expiry, strike, opt_type.upper(), underlying)).fetchone()
        return dict(row) if row else None


# ── helpers ────────────────────────────────────────────────────────────────────

def _serialize(v):
    """Convert legs lists/dicts to JSON strings for storage."""
    if isinstance(v, (list, dict)):
        return json.dumps(v)
    return v

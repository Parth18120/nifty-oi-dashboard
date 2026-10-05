"""Persistence helpers: paper-trade session file, open-LTP snapshot, IV-crush log."""

import json
import os
from datetime import datetime

from oi_dashboard.core import state
from oi_dashboard.core.constants import (
    IST,
    IV_CRUSH_FILE,
    LOT_SIZE,
    OPEN_SNAP_DIR,
    PAPER_TRADES_FILE,
)
from oi_dashboard.core.paths import ROOT


def load_paper_trades():
    try:
        session_file = os.path.join(str(ROOT), PAPER_TRADES_FILE)
        with open(session_file) as f:
            saved = json.load(f)
        today_str  = datetime.now(IST).strftime("%Y-%m-%d")
        saved_date = saved.get("date", "")

        if saved_date and saved_date != today_str and saved.get("trades"):
            # Archive previous day's trades
            archive_dir = os.path.join(str(ROOT), "Paper Trading Data")
            os.makedirs(archive_dir, exist_ok=True)
            archive = os.path.join(archive_dir, f"paper_trades_{saved_date}.json")
            if not os.path.exists(archive):
                import shutil
                shutil.copy(session_file, archive)
                print(f"[PAPER] Archived {saved_date} → Paper Trading Data/")
            state.paper_trades     = []
            state.paper_trade_ids  = set()
            state.paper_trade_date = today_str
            save_paper_trades()
            print(f"[PAPER] New day {today_str} — fresh session")
            return

        if saved_date == today_str:
            state.paper_trades     = saved.get("trades", [])
            state.paper_trade_ids  = set(tuple(x) for x in saved.get("ids", []))
            state.paper_trade_date = saved_date
            # Back-fill fields for trades saved before these fields existed
            for t in state.paper_trades:
                if "entry_reason" not in t:
                    if t.get("carry_forward"):
                        t["entry_reason"] = "carry_forward"
                    elif t.get("manual_entry"):
                        t["entry_reason"] = "manual"
                    else:
                        t["entry_reason"] = "auto"
                if "lot_size_used" not in t:
                    # Trades before 2026-07-15 were at old lot size 75
                    entry_date = (t.get("entry_time") or "")[:10]
                    t["lot_size_used"] = 75 if entry_date < "2026-07-15" else LOT_SIZE
            print(f"[PAPER] Restored {len(state.paper_trades)} trades for {today_str}")
        else:
            print("[PAPER] No session data — fresh start")
    except Exception:
        pass


def save_paper_trades():
    try:
        path = os.path.join(str(ROOT), PAPER_TRADES_FILE)
        with open(path, "w") as f:
            json.dump({
                "trades": state.paper_trades,
                "ids":    [list(x) for x in state.paper_trade_ids],
                "date":   state.paper_trade_date or datetime.now(IST).strftime("%Y-%m-%d"),
            }, f, indent=2, default=str)
    except Exception as e:
        print(f"Warning: could not save paper trades: {e}")


def open_snap_path():
    return os.path.join(OPEN_SNAP_DIR, f"open_snapshot_{datetime.now(IST).strftime('%Y-%m-%d')}.json")


def load_open_ltp_snapshot():
    try:
        os.makedirs(OPEN_SNAP_DIR, exist_ok=True)
        path = open_snap_path()
        if os.path.exists(path):
            with open(path) as f:
                state.open_ltp_snapshot = json.load(f)
            print(f"[SNAP] Loaded open LTP snapshot ({len(state.open_ltp_snapshot)} strikes)")
    except Exception as e:
        print(f"[SNAP] Could not load: {e}")


def save_open_ltp_snapshot():
    try:
        os.makedirs(OPEN_SNAP_DIR, exist_ok=True)
        with open(open_snap_path(), "w") as f:
            json.dump(state.open_ltp_snapshot, f)
    except Exception as e:
        print(f"[SNAP] Could not save: {e}")


def load_iv_crush_log():
    try:
        if os.path.exists(IV_CRUSH_FILE):
            with open(IV_CRUSH_FILE) as f:
                raw = json.load(f)
            state.iv_crush_log = []
            for row in raw:
                # Fix rows stored as decimal (1.525) instead of percentage (15.25)
                if row.get("open_iv", 1) < 1.0:
                    row["open_iv"]  = round(row["open_iv"] * 100, 2)
                    row["close_iv"] = round(row.get("close_iv", 0) * 100, 2)
                    if row.get("crush_pct") is not None:
                        row["crush_pct"] = round(row["crush_pct"] * 100, 2)
                state.iv_crush_log.append(row)
            print(f"[IV] Loaded {len(state.iv_crush_log)} IV crush records")
    except Exception as e:
        print(f"[IV] Could not load iv_crush_log: {e}")


def save_iv_crush_log():
    try:
        with open(IV_CRUSH_FILE, "w") as f:
            json.dump(state.iv_crush_log, f, indent=2, default=str)
    except Exception as e:
        print(f"[IV] Could not save iv_crush_log: {e}")


def save_gap_confidence_log(entry):
    """Append a confidence score entry to data/gap_confidence_log.json."""
    try:
        data_dir  = os.path.join(str(ROOT), "data")
        os.makedirs(data_dir, exist_ok=True)
        log_file  = os.path.join(data_dir, "gap_confidence_log.json")
        log       = []
        try:
            with open(log_file) as f:
                log = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        # Replace today's entry if it exists
        today = datetime.now(IST).strftime("%Y-%m-%d")
        log = [r for r in log if r.get("date") != today]
        log.append(entry)
        with open(log_file, "w") as f:
            json.dump(log[-120:], f, indent=2)  # keep 6 months
    except Exception as e:
        print(f"[GAP LOG] save error: {e}")

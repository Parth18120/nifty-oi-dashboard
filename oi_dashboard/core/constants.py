"""Process-wide constants (timezone, lot size, file locations)."""
import os
from datetime import timedelta, timezone

from oi_dashboard.core.paths import DATA_DIR, ROOT

IST = timezone(timedelta(hours=5, minutes=30))

try:
    from config import SPOT_HISTORY_CSV
    SPOT_HISTORY_FILE = SPOT_HISTORY_CSV or ""
except ImportError:
    SPOT_HISTORY_FILE = ""

try:
    from config import LOT_SIZE, MARGIN_PER_LOT
except ImportError:
    LOT_SIZE = 65
    MARGIN_PER_LOT = 91_000

DEFAULT_LOTS = 10
PAPER_TRADES_FILE = "paper_trades_session.json"

OPEN_SNAP_DIR = str(DATA_DIR)
IV_CRUSH_FILE = str(ROOT / "iv_crush_log.json")

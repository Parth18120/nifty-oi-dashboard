"""Rolling log of AUTO engine events shown in the dashboard mini-panel."""

from datetime import datetime

from oi_dashboard.core import state
from oi_dashboard.core.constants import IST


def log_auto(event_type, msg, legs=None):
    """Append an event to _auto_activity_log (capped at 30 entries, newest first)."""
    entry = {
        "time": datetime.now(IST).strftime("%H:%M:%S"),
        "type": event_type,
        "msg":  msg,
    }
    if legs:
        entry["legs"] = legs
    state.auto_activity_log.insert(0, entry)
    if len(state.auto_activity_log) > 30:
        state.auto_activity_log = state.auto_activity_log[:30]

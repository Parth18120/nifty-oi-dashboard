"""Market-hours helpers and refresh cadence."""

from datetime import datetime

from oi_dashboard.core.constants import IST


def get_cache_seconds():
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return 60
    t = now.hour * 60 + now.minute
    if 9 * 60 + 15 <= t <= 15 * 60 + 30:
        return 5    # 5-second refresh during all market hours
    return 60


def is_market_open():
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return 9 * 60 + 15 <= t <= 15 * 60 + 30

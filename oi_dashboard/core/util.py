"""Small shared helpers."""

from datetime import datetime


def safe_sub(a, b, default=0):
    if a is None or b is None:
        return default
    return a - b


def parse_date_safe(s):
    """Normalizes DD-MM-YYYY, YYYY-MM-DD, D/M/YYYY etc. → date object. Returns None on failure."""
    s = str(s).strip()[:10]
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None

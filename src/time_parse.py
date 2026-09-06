"""Very small helper to pull a time window out of a free-text question like
'what was I working on at 16:00' or 'what was I doing around 4pm yesterday'.

This is intentionally simple — good enough for the ask() tool's time-based
branch. Swap for something sturdier once real usage patterns are known.
"""
import re
from datetime import datetime, timedelta

_TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.IGNORECASE)


def extract_time_window(question: str, window_minutes: int = 20, now: datetime = None):
    """Returns (start_iso, end_iso) centered on the first time mention found,
    or (None, None) if no time expression is present."""
    now = now or datetime.now()
    base_day = now

    if re.search(r"\byesterday\b", question, re.IGNORECASE):
        base_day = now - timedelta(days=1)

    match = _TIME_RE.search(question)
    if not match:
        return None, None

    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    ampm = (match.group(3) or "").lower()

    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0

    if not (0 <= hour <= 23):
        return None, None

    center = base_day.replace(hour=hour, minute=minute, second=0, microsecond=0)
    half = timedelta(minutes=window_minutes // 2)
    start = center - half
    end = center + half
    return start.isoformat(), end.isoformat()

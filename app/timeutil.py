from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.core.config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: datetime) -> datetime:
    """SQLite drops tzinfo; treat naive datetimes as UTC. Naive input from clients is local time (IST)."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def local_to_utc(dt: datetime) -> datetime:
    """A date-time typed by a person with no offset is in the business's timezone."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(get_settings().timezone))
    return dt.astimezone(timezone.utc)

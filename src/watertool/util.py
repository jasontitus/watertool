"""Small shared helpers — time handling above all.

Everything in watertool stores timestamps as ISO-8601 UTC strings so they sort
lexically in SQLite and stay readable. Rachio hands out epoch milliseconds; these
helpers are the single conversion point.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime | str | None) -> str | None:
    """Normalize a datetime (or already-ISO string) to an ISO-8601 UTC string."""
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def from_ms(ms: int | str | None) -> datetime | None:
    """Epoch milliseconds -> aware UTC datetime."""
    if ms is None or ms == "":
        return None
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


def to_ms(dt: datetime) -> int:
    """Aware datetime -> epoch milliseconds (Rachio's event query unit)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def days_ago(days: float) -> datetime:
    return utcnow() - timedelta(days=days)

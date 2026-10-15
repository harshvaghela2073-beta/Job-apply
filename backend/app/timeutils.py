"""Time helpers. Timestamps are stored and returned in UTC; "today" and "this month" use the
server's local timezone."""

from datetime import UTC, date, datetime, time, timedelta


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_utc(value: datetime) -> datetime:
    """Return ``value`` as an aware UTC datetime. Naive values are assumed to already be UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def iso_utc(value: datetime) -> str:
    """Format as ISO 8601 UTC with a ``Z`` suffix, e.g. ``2026-10-13T06:45:00Z``."""
    return to_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_midnight(day: date) -> datetime:
    # A naive datetime passed to astimezone() is interpreted as server local time.
    return datetime.combine(day, time.min).astimezone()


def local_today(now: datetime | None = None) -> date:
    return (now or utcnow()).astimezone().date()


def local_day_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    """UTC [start, end) of the current local day."""
    today = local_today(now)
    start = _local_midnight(today)
    end = _local_midnight(today + timedelta(days=1))
    return start.astimezone(UTC), end.astimezone(UTC)


def local_month_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    """UTC [start, end) of the current local calendar month."""
    first = local_today(now).replace(day=1)
    next_first = (first + timedelta(days=32)).replace(day=1)
    return _local_midnight(first).astimezone(UTC), _local_midnight(next_first).astimezone(UTC)

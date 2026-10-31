"""Windows (UTC): the day window starts at UTC midnight."""

import math
from datetime import UTC, date, datetime, time, timedelta

from app.ai import quota


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_today(now: datetime | None = None) -> date:
    return (now or quota.utc_now()).astimezone(UTC).date()


def months_before(day: date, months: int) -> date:
    """The first day of the month ``months`` calendar months before ``day``'s."""
    index = day.year * 12 + (day.month - 1) - months
    return date(index // 12, index % 12 + 1, 1)


def seconds_until_utc_midnight(now: datetime | None = None) -> int:
    """Whole seconds until the next UTC midnight, at least 1 (for Retry-After)."""
    now = (now or quota.utc_now()).astimezone(UTC)
    midnight = datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=UTC)
    return max(1, int((midnight - now).total_seconds()))


def lease_seconds(timeout_s: float) -> int:
    return math.ceil(timeout_s) + quota.LEASE_GRACE_SECONDS

"""LEGACY (#509) daily scan caps: per-user reservation, global ceiling, refund
and retention.

Since #528 ``/scan`` no longer calls this module: allowances, the in-flight
lease and the daily window live in ``app/ai/quota.py`` (table
``ai_quota``). It is kept, with its ``scan_usage_daily`` table and purge, until
#540 deletes it.

Every scan that reaches the vision provider is counted in ``scan_usage_daily``
(one row per user per UTC day).

- **Per-user cap (exact).** :func:`reserve_scan` is a single conditional upsert:
  the row is created with ``scans = 1`` or incremented only while
  ``scans < limit``. Postgres serialises concurrent upserts on the same
  (user_id, usage_date) key, so no more than ``limit`` reservations can ever
  succeed for one user on one day.
- **Global cap (approximate).** :func:`global_scans_today` sums today's rows
  and the endpoint compares it to the ceiling *before* reserving. Requests that
  read the sum at the same moment can each pass, so the global total can
  overshoot by at most the number of in-flight requests. That is accepted:
  the per-user cap and the rate limit bound how large that window can be.
- **Refund.** :func:`refund_scan` gives a reservation back when the provider
  reports itself unavailable (``ScanUnavailableError``) or the connection was
  never established (``httpx.ConnectError``), never taking the count below zero.
- **Retention.** :func:`purge_old_scan_usage` deletes rows older than
  ``RETENTION_DAYS`` in bounded batches; it runs from the daily cleanup task.
"""

import logging
import uuid
from datetime import UTC, date, datetime, time, timedelta

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.scan_usage import ScanUsageDaily

logger = logging.getLogger(__name__)

RETENTION_DAYS = 90
PURGE_BATCH_SIZE = 5_000
# Upper bound on batches per cleanup run, so one run can't hold the database
# for long even after a long outage of the cleanup task.
PURGE_MAX_BATCHES = 20


def utc_today(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(UTC).date()


def seconds_until_utc_midnight(now: datetime | None = None) -> int:
    """Whole seconds until the next UTC midnight, at least 1 (for Retry-After)."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    midnight = datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=UTC)
    return max(1, int((midnight - now).total_seconds()))


async def global_scans_today(db: AsyncSession, day: date) -> int:
    result = await db.execute(
        select(func.coalesce(func.sum(ScanUsageDaily.scans), 0)).where(
            ScanUsageDaily.usage_date == day
        )
    )
    return int(result.scalar_one())


async def reserve_scan(
    db: AsyncSession, user_id: uuid.UUID, day: date, limit: int
) -> int | None:
    """Reserve one scan for ``user_id`` on ``day`` if under ``limit``.

    Returns the user's new count for the day, or ``None`` when the limit is
    already reached (nothing is changed then). Commits immediately so the
    reservation is visible to concurrent requests.
    """
    stmt = (
        insert(ScanUsageDaily)
        .values(user_id=user_id, usage_date=day, scans=1)
        .on_conflict_do_update(
            index_elements=[ScanUsageDaily.user_id, ScanUsageDaily.usage_date],
            set_={"scans": ScanUsageDaily.scans + 1},
            where=ScanUsageDaily.scans < limit,
        )
        .returning(ScanUsageDaily.scans)
    )
    result = await db.execute(stmt)
    scans = result.scalar_one_or_none()
    await db.commit()
    return int(scans) if scans is not None else None


async def refund_scan(db: AsyncSession, user_id: uuid.UUID, day: date) -> None:
    """Give back one reservation; the count never goes below zero."""
    await db.execute(
        update(ScanUsageDaily)
        .where(
            ScanUsageDaily.user_id == user_id,
            ScanUsageDaily.usage_date == day,
            ScanUsageDaily.scans > 0,
        )
        .values(scans=ScanUsageDaily.scans - 1)
    )
    await db.commit()


async def purge_old_scan_usage(db: AsyncSession, today: date | None = None) -> int:
    """Delete rows older than RETENTION_DAYS in bounded batches; return the count."""
    cutoff = (today or utc_today()) - timedelta(days=RETENTION_DAYS)
    total = 0
    for _ in range(PURGE_MAX_BATCHES):
        batch = (
            select(ScanUsageDaily.user_id, ScanUsageDaily.usage_date)
            .where(ScanUsageDaily.usage_date < cutoff)
            .limit(PURGE_BATCH_SIZE)
        )
        result = await db.execute(
            delete(ScanUsageDaily).where(
                tuple_(ScanUsageDaily.user_id, ScanUsageDaily.usage_date).in_(batch)
            )
        )
        await db.commit()
        deleted = result.rowcount or 0
        total += deleted
        if deleted < PURGE_BATCH_SIZE:
            break
    return total


async def reserve_daily_scan(db: AsyncSession, user, usage_day: date) -> None:
    """LEGACY (moved from ``app/api/scan.py`` in #528; not called by /scan).

    Enforce the global and per-user daily caps, reserving one scan, on the
    caller's session. ``reserve_scan`` commits, which makes the reservation
    visible to concurrent requests and hands the connection back to the pool.
    """
    used_globally = await global_scans_today(db, usage_day)
    if used_globally >= settings.scan_global_daily_limit:
        logger.warning("scan: global daily limit reached (%d)", used_globally)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="scan_capacity",
        )
    reserved = await reserve_scan(
        db, user.id, usage_day, settings.scan_daily_limit_per_user
    )
    if reserved is None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="daily_scan_limit",
            headers={"Retry-After": str(seconds_until_utc_midnight())},
        )


async def refund_daily_scan(db: AsyncSession, user, usage_day: date) -> None:
    """LEGACY (moved from ``app/api/scan.py`` in #528). Give back a
    reservation; a failure here must not mask the original error."""
    try:
        await refund_scan(db, user.id, usage_day)
    except Exception:
        logger.exception("scan: failed to refund daily scan reservation")
        try:
            await db.rollback()
        except Exception:
            logger.exception("scan: rollback after failed refund also failed")

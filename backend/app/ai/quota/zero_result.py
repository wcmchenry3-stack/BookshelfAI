"""Zero-result enhanced scans (#533): the throttle and the record."""

import logging
import math
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import quota
from app.ai.quota.types import Reservation, ZeroResultRecorded
from app.models.ai_enhanced_zero_result import AIEnhancedZeroResult

logger = logging.getLogger(__package__)


def _in_window(user_id: uuid.UUID, window_hours: int):
    # The window is sliding and measured on the database clock, the same
    # clock that stamps the rows (record_zero_result).
    return and_(
        AIEnhancedZeroResult.user_id == user_id,
        AIEnhancedZeroResult.created_at
        > quota._clock_timestamp() - timedelta(hours=window_hours),
    )


async def zero_result_count(
    db: AsyncSession, user_id: uuid.UUID, window_hours: int
) -> int:
    """The user's uncharged zero-result enhanced scans inside the window (no
    commit)."""
    result = await db.execute(
        select(func.count())
        .select_from(AIEnhancedZeroResult)
        .where(quota._in_window(user_id, window_hours))
    )
    return int(result.scalar_one())


def retry_after_seconds(
    leaves_at: datetime | None, window_hours: int, now: datetime | None = None
) -> int:
    """Whole seconds until ``leaves_at`` (when the row that must leave the
    window does), at least 1. Without a row (threshold 0: every enhanced scan
    is refused) the whole window."""
    if leaves_at is None:
        return max(1, window_hours * 3600)
    now = (now or quota.utc_now()).astimezone(UTC)
    return max(1, math.ceil((leaves_at - now).total_seconds()))


async def zero_result_throttle(
    db: AsyncSession, user_id: uuid.UUID, threshold: int, window_hours: int
) -> int | None:
    """``None`` while the user's in-window count is below ``threshold``;
    otherwise the Retry-After seconds: until the row whose leaving brings the
    count back below the threshold (the ``count - threshold + 1``-th oldest)
    leaves the window. Ends its read transaction."""
    try:
        count = await quota.zero_result_count(db, user_id, window_hours)
        if count < threshold:
            return None
        leaves_at = None
        if count:
            result = await db.execute(
                select(AIEnhancedZeroResult.created_at)
                .where(quota._in_window(user_id, window_hours))
                .order_by(AIEnhancedZeroResult.created_at)
                .offset(count - threshold)
                .limit(1)
            )
            oldest = result.scalar_one_or_none()
            if oldest is not None:
                leaves_at = oldest + timedelta(hours=window_hours)
        return quota.retry_after_seconds(leaves_at, window_hours)
    finally:
        # Read only; do not leave the connection idle in a transaction.
        await db.commit()


def zero_result_charged(count: int, threshold: int) -> bool:
    """Whether a zero-result enhanced scan is past the threshold, given the
    user's in-window count before it: at or above the threshold nothing is
    recorded (threshold 0: never recorded). Named for the #533 rule, when
    below the threshold also gave back the enhanced monthly unit (#582
    removed that unit)."""
    return count >= threshold


async def warned_in_window(
    db: AsyncSession, user_id: uuid.UUID, window_hours: int
) -> bool:
    """True when a row of the user that sent the warning is inside the
    window (no commit)."""
    result = await db.execute(
        select(AIEnhancedZeroResult.id)
        .where(quota._in_window(user_id, window_hours), AIEnhancedZeroResult.warned)
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def record_zero_result(
    db: AsyncSession, reservation: "Reservation", ledger_id: uuid.UUID | None
) -> ZeroResultRecorded | None:
    """The zero-result record, inside the caller's settle transaction (no
    commit). Returns the user's new in-window count when the scan is below
    the threshold (one row written), or ``None`` when it is at or above it
    (nothing written). The daily unit stays charged either way: there is no
    enhanced allowance to give back since #582.

    The warning claim: when the new row brings the count to the threshold
    and no row of the user with ``warned = true`` is inside the window, the
    new row is written with ``warned = true`` and ``warn`` is returned True.
    The claim commits with the row, so at most one warning per user per
    window; the caller sends it after the commit. If that commit's
    acknowledgement is lost, the retried settle finds the ledger row (or no
    lease) and records nothing, so no warning is sent: that window's warning
    is lost (fails toward fewer alerts; "at most one" still holds).

    Serialisation: before counting, the request's own lease row is locked
    (``SELECT ... FROM ai_inflight WHERE user_id AND lease_id FOR UPDATE``).
    The settle deletes that row later in the same transaction, so the lock
    is held until the commit, and a second settle of the same user (a request
    whose lease expired and was taken over) cannot find it: it writes no row
    and sends no warning. Without its lease, nothing is ever
    recorded. That includes the lease-only paths (``settle_after_lease``: a
    retry without a ledger row, and the fallback after a rejected ledger
    row), which delete the lease first, so they record nothing.

    Runs in a SAVEPOINT: if it fails, only it is rolled back (no row) and
    the settle still commits the usage row, the cost and the lease delete.

    A threshold lowered below a user's current in-window count means no row
    reaches it exactly, so no warning is sent for that user in that window
    (they are still throttled with ``throttle``).
    """
    try:
        async with db.begin_nested():
            if not await quota.hold_lease_lock(
                db, reservation.user_id, reservation.lease_id
            ):
                return None
            count = await quota.zero_result_count(
                db, reservation.user_id, reservation.zero_result_window_hours
            )
            if quota.zero_result_charged(count, reservation.zero_result_threshold):
                return None
            new_count = count + 1
            warn = (
                new_count == reservation.zero_result_threshold
                and not await quota.warned_in_window(
                    db, reservation.user_id, reservation.zero_result_window_hours
                )
            )
            await db.execute(
                insert(AIEnhancedZeroResult).values(
                    id=uuid.uuid4(),
                    user_id=reservation.user_id,
                    created_at=quota._clock_timestamp(),
                    ledger_id=ledger_id,
                    warned=warn,
                )
            )
            return ZeroResultRecorded(new_count, warn)
    except Exception:
        logger.exception("ai_quota: zero-result record failed; no row written")
        return None

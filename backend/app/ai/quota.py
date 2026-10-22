"""Per-user AI allowances: reserve before the provider, settle or refund after.

Every paid scan goes through :func:`reserve` before the provider is called.
In order (#528):

1. **Entitlement.** The tier comes from ``entitlements.tier_for(db, user)``
   (server-side only); the allowances come from ``ai_settings``
   (``quota.<tier>.daily`` / ``quota.<tier>.monthly``). Nothing in the request
   can change either.
2. **Global ceiling** (``scan.global_daily_limit``, approximate). Today's day
   rows are summed across users and features and compared before anything is
   reserved, so no per-user row or lease is taken when the service-wide
   capacity is gone (:class:`GlobalCapacityReached`). Concurrent requests can
   overshoot by at most the number in flight; the lease bounds that to one per
   user.
3. **In-flight lease** (table ``ai_inflight``). One conditional upsert:
   ``INSERT ... ON CONFLICT (user_id) DO UPDATE ... WHERE
   ai_inflight.lease_expires_at < clock_timestamp()``. It succeeds only when
   the user has no row or the old lease has expired, so at most one paid call
   per user is in flight (:class:`ScanInProgress`). The lease lasts
   ``timeout_s + 30`` seconds, so a crashed request frees it. Each lease has a
   random ``lease_id``; :func:`release` deletes the row only while it still
   holds that id, so a slow request whose lease expired and was taken over
   cannot delete the new holder's lease. After the reservations below the
   lease is renewed (:func:`renew_lease`), so the provider call gets the full
   lease; if a stall let another request take it over, the reservation is
   given back and :class:`ScanInProgress` raised.
4. **Daily, then monthly reservation** (table ``ai_quota``). The
   same exact conditional upsert as #509: ``units_used = units_used + 1 WHERE
   units_used < limit``. Postgres serialises upserts on the same key, so no
   more than ``limit`` reservations can succeed (:class:`DailyLimitReached`,
   :class:`MonthlyLimitReached`). A limit of 0 refuses without writing.
   Standard and enhanced scans share one allowance (OD-1): both reserve under
   :data:`SCAN_QUOTA_FEATURE`. The ``feature`` column stays so later AI
   features can have their own allowance.

Every step commits straight away, on the request's own session: the
reservation is visible to concurrent requests and the connection goes back to
the pool before the provider is called (as in #509/#519). A refusal or an
error at any step gives back what was already taken (the daily reservation,
the lease) before it propagates.

After the provider call the caller must always :func:`release` the lease (in a
``finally``, even on cancellation; the release is shielded). On
``ScanUnavailableError`` / ``httpx.ConnectError`` it first calls
:func:`refund`, which gives back the daily and monthly units, never below
zero. Any other outcome (success, an empty result, another error, a cancelled
request) keeps the reservation: the provider may have been paid.

Windows are UTC: the day window starts at UTC midnight, the month window on
the 1st of the UTC calendar month (OD-10). ``period_end`` is the first day of
the next month, the date the monthly count resets.

Retention: :func:`purge_old_quota` deletes day rows older than 90 days and
month rows older than 13 months, in bounded batches, from the daily cleanup
task.

This module never calls a provider and must not import ``app.ai.providers``.
"""

import logging
import math
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

import anyio
from sqlalchemy import DateTime, and_, delete, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import entitlements
from app.ai.entitlements import Tier
from app.ai.settings import AISettings
from app.models.ai_quota import WINDOW_DAY, WINDOW_MONTH, AIInflight, AIQuota
from app.models.user import User

logger = logging.getLogger(__name__)

# The ai_quota counter key for every scan, standard or enhanced: one shared
# daily and monthly allowance per user (OD-1). Not a gateway feature name.
SCAN_QUOTA_FEATURE = "scan"

# The lease outlives the provider timeout by this much (AC: timeout_s + 30).
LEASE_GRACE_SECONDS = 30
# Seconds a client should wait before retrying while another scan is running.
IN_PROGRESS_RETRY_AFTER_SECONDS = 2
# Bound on each give-back (refund, lease release) so cleanup can't hang a
# request; the lease expiry is the backstop.
CLEANUP_TIMEOUT_SECONDS = 5.0

DAILY_RETENTION_DAYS = 90
MONTHLY_RETENTION_MONTHS = 13
PURGE_BATCH_SIZE = 5_000
# Upper bound on batches per window kind per cleanup run.
PURGE_MAX_BATCHES = 20


# ---------------------------------------------------------------------------
# Windows (UTC)
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_today(now: datetime | None = None) -> date:
    return (now or utc_now()).astimezone(UTC).date()


def month_start(day: date) -> date:
    return day.replace(day=1)


def next_month_start(day: date) -> date:
    """First day of the month after ``day``'s month (Dec -> Jan next year)."""
    if day.month == 12:
        return date(day.year + 1, 1, 1)
    return date(day.year, day.month + 1, 1)


def period_end(day: date) -> date:
    """The date the monthly allowance resets: the 1st of the next UTC month."""
    return next_month_start(day)


def months_before(day: date, months: int) -> date:
    """The first day of the month ``months`` calendar months before ``day``'s."""
    index = day.year * 12 + (day.month - 1) - months
    return date(index // 12, index % 12 + 1, 1)


def seconds_until_utc_midnight(now: datetime | None = None) -> int:
    """Whole seconds until the next UTC midnight, at least 1 (for Retry-After)."""
    now = (now or utc_now()).astimezone(UTC)
    midnight = datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=UTC)
    return max(1, int((midnight - now).total_seconds()))


def lease_seconds(timeout_s: float) -> int:
    return math.ceil(timeout_s) + LEASE_GRACE_SECONDS


# ---------------------------------------------------------------------------
# Allowances
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Allowance:
    daily: int
    monthly: int


def allowance_for(ai: AISettings, tier: Tier) -> Allowance:
    """The tier's allowance from ``ai_settings`` (validated, bounded)."""
    daily, monthly = ai.allowance(tier.value)
    return Allowance(daily=daily, monthly=monthly)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


class QuotaRefusal(Exception):
    """A scan was refused before the provider; nothing is left reserved."""


class GlobalCapacityReached(QuotaRefusal):
    """The service-wide daily ceiling is used up (503 ``scan_capacity``)."""


class ScanInProgress(QuotaRefusal):
    """Another paid call for this user holds the lease (429 ``scan_in_progress``)."""

    retry_after = IN_PROGRESS_RETRY_AFTER_SECONDS


class DailyLimitReached(QuotaRefusal):
    """The user's daily allowance is used (429 ``daily_scan_limit``)."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("daily_scan_limit")
        self.retry_after = retry_after


class MonthlyLimitReached(QuotaRefusal):
    """The user's monthly allowance is used (402 ``scan_limit_reached``)."""

    def __init__(self, limit: int, used: int, period_end: date) -> None:
        super().__init__("scan_limit_reached")
        self.limit = limit
        self.used = used
        self.period_end = period_end


@dataclass(frozen=True)
class Reservation:
    """What :func:`reserve` took; hand it to :func:`refund` and :func:`release`."""

    user_id: uuid.UUID
    feature: str
    lease_id: uuid.UUID
    day: date
    month: date
    tier: Tier
    allowance: Allowance


# ---------------------------------------------------------------------------
# Primitives (each commits)
# ---------------------------------------------------------------------------


def _clock_timestamp():
    # clock_timestamp(), not now(): now() is the transaction start, and the
    # request's transaction may have been open since authentication.
    return func.clock_timestamp(type_=DateTime(timezone=True))


async def acquire_lease(
    db: AsyncSession, user_id: uuid.UUID, seconds: int
) -> uuid.UUID | None:
    """Take the user's in-flight lease for ``seconds``; return its id, or
    ``None`` when another unexpired lease is held (nothing changes then)."""
    lease_id = uuid.uuid4()
    stmt = insert(AIInflight).values(
        user_id=user_id,
        lease_id=lease_id,
        lease_expires_at=_clock_timestamp() + timedelta(seconds=seconds),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[AIInflight.user_id],
        set_={
            "lease_id": stmt.excluded.lease_id,
            "lease_expires_at": stmt.excluded.lease_expires_at,
        },
        where=AIInflight.lease_expires_at < _clock_timestamp(),
    ).returning(AIInflight.lease_id)
    result = await db.execute(stmt)
    acquired = result.scalar_one_or_none()
    await db.commit()
    return acquired if acquired == lease_id else None


async def renew_lease(
    db: AsyncSession, user_id: uuid.UUID, lease_id: uuid.UUID, seconds: int
) -> bool:
    """Restart the lease's clock only if ``lease_id`` still holds it; True if
    renewed. False means the lease expired and another request took it."""
    result = await db.execute(
        update(AIInflight)
        .where(AIInflight.user_id == user_id, AIInflight.lease_id == lease_id)
        .values(lease_expires_at=_clock_timestamp() + timedelta(seconds=seconds))
        .returning(AIInflight.lease_id)
    )
    renewed = result.scalar_one_or_none() is not None
    await db.commit()
    return renewed


async def release_lease(
    db: AsyncSession, user_id: uuid.UUID, lease_id: uuid.UUID
) -> bool:
    """Delete the lease only if ``lease_id`` still holds it; True if deleted."""
    result = await db.execute(
        delete(AIInflight).where(
            AIInflight.user_id == user_id, AIInflight.lease_id == lease_id
        )
    )
    await db.commit()
    return bool(result.rowcount)


async def reserve_unit(
    db: AsyncSession,
    user_id: uuid.UUID,
    feature: str,
    window_kind: str,
    window_start: date,
    limit: int,
) -> int | None:
    """Reserve one unit in the window if under ``limit``; return the new count,
    or ``None`` when the limit is reached (nothing changes then)."""
    if limit <= 0:
        # The insert path would create the row at 1; a zero allowance must
        # refuse without writing.
        return None
    stmt = (
        insert(AIQuota)
        .values(
            user_id=user_id,
            feature=feature,
            window_kind=window_kind,
            window_start=window_start,
            units_used=1,
        )
        .on_conflict_do_update(
            index_elements=[
                AIQuota.user_id,
                AIQuota.feature,
                AIQuota.window_kind,
                AIQuota.window_start,
            ],
            set_={"units_used": AIQuota.units_used + 1},
            where=AIQuota.units_used < limit,
        )
        .returning(AIQuota.units_used)
    )
    result = await db.execute(stmt)
    used = result.scalar_one_or_none()
    await db.commit()
    return int(used) if used is not None else None


async def refund_unit(
    db: AsyncSession,
    user_id: uuid.UUID,
    feature: str,
    window_kind: str,
    window_start: date,
) -> None:
    """Give back one unit; the count never goes below zero (and no row is
    created)."""
    await db.execute(
        update(AIQuota)
        .where(
            AIQuota.user_id == user_id,
            AIQuota.feature == feature,
            AIQuota.window_kind == window_kind,
            AIQuota.window_start == window_start,
            AIQuota.units_used > 0,
        )
        .values(units_used=AIQuota.units_used - 1)
    )
    await db.commit()


async def units_used(
    db: AsyncSession,
    user_id: uuid.UUID,
    feature: str,
    window_kind: str,
    window_start: date,
) -> int:
    result = await db.execute(
        select(AIQuota.units_used).where(
            AIQuota.user_id == user_id,
            AIQuota.feature == feature,
            AIQuota.window_kind == window_kind,
            AIQuota.window_start == window_start,
        )
    )
    return int(result.scalar_one_or_none() or 0)


async def global_units_today(db: AsyncSession, day: date) -> int:
    """Units reserved on ``day`` across all users and features."""
    result = await db.execute(
        select(func.coalesce(func.sum(AIQuota.units_used), 0)).where(
            AIQuota.window_kind == WINDOW_DAY, AIQuota.window_start == day
        )
    )
    return int(result.scalar_one())


# ---------------------------------------------------------------------------
# Give-backs: never raise, bounded, shielded from cancellation
# ---------------------------------------------------------------------------


async def _give_back(
    db: AsyncSession, what: str, op: Callable[[], Awaitable[object]]
) -> None:
    """Run a give-back so that it cannot mask the original outcome.

    Shielded so it still runs while the request is being cancelled (anyio
    cancellation, as on the request path; a native ``asyncio`` task cancel,
    e.g. at server shutdown, cuts through the shield), and bounded by ``CLEANUP_TIMEOUT_SECONDS``. A failure is logged and retried
    once after a rollback (the session may be in a failed transaction after
    the error being handled); a second failure is logged and swallowed: the
    lease expiry is the backstop.

    If the bound cuts a query off mid-execute, SQLAlchemy invalidates the
    session's connection and every later query on the request session would
    raise ``PendingRollbackError``. So after a timeout the session is rolled
    back (also shielded and bounded; a failure there is logged and swallowed).
    """
    with anyio.move_on_after(CLEANUP_TIMEOUT_SECONDS, shield=True) as scope:
        for attempt in (1, 2):
            try:
                await op()
                return
            except Exception:
                logger.exception("ai_quota: %s failed (attempt %d)", what, attempt)
                try:
                    await db.rollback()
                except Exception:
                    logger.exception("ai_quota: rollback after failed %s", what)
    if scope.cancelled_caught:
        logger.error("ai_quota: %s timed out", what)
        with anyio.move_on_after(CLEANUP_TIMEOUT_SECONDS, shield=True) as rb_scope:
            try:
                await db.rollback()
            except Exception:
                logger.exception("ai_quota: rollback after timed-out %s", what)
        if rb_scope.cancelled_caught:
            logger.error("ai_quota: rollback after timed-out %s timed out", what)


async def release(db: AsyncSession, reservation: Reservation) -> None:
    """Release the lease held by ``reservation`` (settle). Never raises."""
    await _give_back(
        db,
        "lease release",
        lambda: release_lease(db, reservation.user_id, reservation.lease_id),
    )


async def _refund_window(
    db: AsyncSession, reservation: Reservation, window_kind: str, start: date
) -> None:
    await _give_back(
        db,
        f"{window_kind} refund",
        lambda: refund_unit(
            db, reservation.user_id, reservation.feature, window_kind, start
        ),
    )


async def refund(db: AsyncSession, reservation: Reservation) -> None:
    """Give back the daily and monthly units (provider unavailable or never
    reached). Never raises; does not release the lease (call :func:`release`)."""
    await _refund_window(db, reservation, WINDOW_DAY, reservation.day)
    await _refund_window(db, reservation, WINDOW_MONTH, reservation.month)


# ---------------------------------------------------------------------------
# Reserve
# ---------------------------------------------------------------------------


async def reserve(
    db: AsyncSession,
    *,
    user: User,
    feature: str,
    timeout_s: float,
    ai: AISettings,
    now: datetime | None = None,
) -> Reservation:
    """Check the allowances and take the lease, a daily and a monthly unit.

    Raises a :class:`QuotaRefusal` (after giving back whatever was taken) when
    the call must not go ahead. Any other exception also gives back what was
    taken before it propagates. On success the caller owns the reservation and
    must :func:`release` it after the provider call.
    """
    tier = await entitlements.tier_for(db, user)
    allowance = allowance_for(ai, tier)
    today = utc_today(now)
    month = month_start(today)

    if await global_units_today(db, today) >= ai.global_daily_limit:
        raise GlobalCapacityReached("scan_capacity")

    seconds = lease_seconds(timeout_s)
    lease_id = await acquire_lease(db, user.id, seconds)
    if lease_id is None:
        raise ScanInProgress("scan_in_progress")
    reservation = Reservation(
        user_id=user.id,
        feature=feature,
        lease_id=lease_id,
        day=today,
        month=month,
        tier=tier,
        allowance=allowance,
    )

    daily_taken = monthly_taken = False
    try:
        if (
            await reserve_unit(db, user.id, feature, WINDOW_DAY, today, allowance.daily)
            is None
        ):
            raise DailyLimitReached(retry_after=seconds_until_utc_midnight(now))
        daily_taken = True
        if (
            await reserve_unit(
                db, user.id, feature, WINDOW_MONTH, month, allowance.monthly
            )
            is None
        ):
            used = await units_used(db, user.id, feature, WINDOW_MONTH, month)
            raise MonthlyLimitReached(
                limit=allowance.monthly, used=used, period_end=period_end(today)
            )
        monthly_taken = True
        # The upserts above are unbounded, and the lease aged while they ran.
        # Restart its clock so the whole timeout_s + 30 s covers the provider
        # call, and stop if a stall let it expire and another request take it.
        if not await renew_lease(db, user.id, lease_id, seconds):
            raise ScanInProgress("scan_in_progress")
    except BaseException:
        # Includes CancelledError: give back what was taken, then propagate.
        if monthly_taken:
            await _refund_window(db, reservation, WINDOW_MONTH, month)
        if daily_taken:
            await _refund_window(db, reservation, WINDOW_DAY, today)
        await release(db, reservation)
        raise
    return reservation


# ---------------------------------------------------------------------------
# Usage (GET /scan/usage)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Usage:
    used_today: int
    daily_limit: int
    used_month: int
    monthly_limit: int
    period_end: date


async def usage_for(
    db: AsyncSession,
    *,
    user: User,
    feature: str,
    ai: AISettings,
    now: datetime | None = None,
) -> Usage:
    tier = await entitlements.tier_for(db, user)
    allowance = allowance_for(ai, tier)
    today = utc_today(now)
    month = month_start(today)
    result = await db.execute(
        select(AIQuota.window_kind, AIQuota.units_used).where(
            AIQuota.user_id == user.id,
            AIQuota.feature == feature,
            or_(
                and_(AIQuota.window_kind == WINDOW_DAY, AIQuota.window_start == today),
                and_(
                    AIQuota.window_kind == WINDOW_MONTH,
                    AIQuota.window_start == month,
                ),
            ),
        )
    )
    used = {kind: int(n) for kind, n in result.all()}
    return Usage(
        used_today=used.get(WINDOW_DAY, 0),
        daily_limit=allowance.daily,
        used_month=used.get(WINDOW_MONTH, 0),
        monthly_limit=allowance.monthly,
        period_end=period_end(today),
    )


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


async def _purge_kind(db: AsyncSession, window_kind: str, cutoff: date) -> int:
    total = 0
    for _ in range(PURGE_MAX_BATCHES):
        batch = (
            select(
                AIQuota.user_id,
                AIQuota.feature,
                AIQuota.window_kind,
                AIQuota.window_start,
            )
            .where(AIQuota.window_kind == window_kind, AIQuota.window_start < cutoff)
            .limit(PURGE_BATCH_SIZE)
        )
        result = await db.execute(
            delete(AIQuota).where(
                tuple_(
                    AIQuota.user_id,
                    AIQuota.feature,
                    AIQuota.window_kind,
                    AIQuota.window_start,
                ).in_(batch)
            )
        )
        await db.commit()
        deleted = result.rowcount or 0
        total += deleted
        if deleted < PURGE_BATCH_SIZE:
            break
    return total


def purge_cutoffs(today: date) -> tuple[date, date]:
    """``(day_cutoff, month_cutoff)``: rows with ``window_start`` before these
    are purged. Day rows are kept for 90 days; month rows for the current
    month and the 13 before it."""
    return (
        today - timedelta(days=DAILY_RETENTION_DAYS),
        months_before(today, MONTHLY_RETENTION_MONTHS),
    )


async def purge_old_quota(db: AsyncSession, today: date | None = None) -> int:
    """Delete old day and month rows in bounded batches; return the count."""
    day_cutoff, month_cutoff = purge_cutoffs(today or utc_today())
    deleted = await _purge_kind(db, WINDOW_DAY, day_cutoff)
    deleted += await _purge_kind(db, WINDOW_MONTH, month_cutoff)
    return deleted

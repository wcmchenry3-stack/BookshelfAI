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
3. **Daily cost ceiling** (#531, ``app/ai/breaker.py``). The feature's
   estimate (``est_cost_micros``) is added to today's ``ai_spend_daily`` row
   with one conditional upsert. For Free it succeeds only while the day's
   reserved plus settled spend stays within
   ``breaker.free_daily_ceiling_micros`` (:class:`CostCeilingReached`, 503
   ``ai_budget_reached``); Premium always adds its estimate (decision #544). It
   sits before the lease, so nothing per-user is taken when the service is
   out of budget, and every later refusal or error gives it back.
4. **In-flight lease** (table ``ai_inflight``). One conditional upsert:
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
5. **Daily, then monthly reservation** (table ``ai_quota``). The
   same exact conditional upsert as #509: ``units_used = units_used + 1 WHERE
   units_used < limit``. Postgres serialises upserts on the same key, so no
   more than ``limit`` reservations can succeed (:class:`DailyLimitReached`,
   :class:`MonthlyLimitReached`). A limit of 0 refuses without writing.
   Standard and enhanced scans share one allowance (OD-1): both reserve under
   :data:`SCAN_QUOTA_FEATURE`. The ``feature`` column stays so later AI
   features can have their own allowance.

**Enhanced scans (#533).** ``reserve(..., enhanced=True)`` adds three steps:

- right after the tier is resolved, a non-Premium tier is refused
  (:class:`EnhancedRequiresPremium`, 403 ``enhanced_requires_premium``) before
  anything is taken (no cost estimate, lease or unit);
- after the lease, with ``scan.enhanced.zero_result_action`` = ``throttle``,
  the user's uncharged zero-result enhanced scans inside the sliding window
  (``ai_enhanced_zero_results``) are counted; at or above
  ``scan.enhanced.zero_result_threshold`` the scan is refused
  (:class:`EnhancedThrottled`, 429 ``enhanced_throttled``) with a Retry-After
  of the seconds until enough rows leave the window. Counting under the lease
  serialises it with the user's own settle;
- after the shared daily and monthly units, one unit of the Premium enhanced
  monthly allowance (``quota.premium.enhanced_monthly``) under
  :data:`ENHANCED_QUOTA_FEATURE` (:class:`EnhancedLimitReached`, 402
  ``enhanced_limit_reached``).

Each refusal gives back everything taken before it, like the others. The
enhanced unit follows the shared units on refund (:func:`refund`) and is kept
otherwise, except for the charge decision: when the provider answered but
returned no books (no candidates), the caller passes a :class:`ZeroResult` to
:func:`release`, and in the settle transaction (gated exactly like the cost
settle, so a retried settle cannot apply it twice) the user's in-window count
is read: below the threshold the enhanced unit is given back and one
``ai_enhanced_zero_results`` row is written (not charged); at or above it the
unit is kept (charged) and no row is written. The write runs in a SAVEPOINT,
so a failure there keeps the unit (charged) and never drops the usage row.
When a row brings the count to the threshold and no row of the user with
``warned = true`` is inside the window, that row is written with ``warned =
true`` (the claim commits with it) and one Sentry warning is sent after the
commit (:func:`send_zero_result_warning`): at most one per user per window.
Every other give-back of the enhanced unit (:func:`refund`, a refusal in
:func:`reserve`) is attempted once only: ``refund_unit`` cannot show whether a
failed attempt committed, and a repeat would take a second unit off, so a
failure leaves the user charged. Threshold 0: with
``charge`` every zero-result enhanced scan is charged (no rows, no warning);
with ``throttle`` every enhanced scan is refused. A future result-cache hit
(#534) makes no provider call and must not pass a :class:`ZeroResult`.

Every step commits straight away, on the request's own session: the
reservation is visible to concurrent requests and the connection goes back to
the pool before the provider is called (as in #509/#519). A refusal or an
error at any step gives back what was already taken (the cost estimate, the
daily reservation, the lease) before it propagates.

After the provider call the caller must always :func:`release` the lease (in a
``finally``, even on cancellation; the release is shielded). The caller passes
the usage-ledger row for the call (#530) and the call's
:class:`~app.ai.breaker.Settlement` (#531); the ledger row and the cost settle
are committed together with the lease delete. Without a settlement the cost
estimate is given back. On
``ScanUnavailableError`` / ``httpx.ConnectError`` it first calls
:func:`refund`, which gives back the daily and monthly units, never below
zero. Any other outcome (success, an empty result, another error, a cancelled
request) keeps the reservation: the provider may have been paid.

Windows are UTC: the day window starts at UTC midnight, the month window on
the 1st of the UTC calendar month (OD-10). ``period_end`` is the first day of
the next month, the date the monthly count resets.

Retention: :func:`purge_old_quota` deletes day rows older than 90 days and
month rows older than 13 months, and :func:`purge_old_zero_results` deletes
``ai_enhanced_zero_results`` rows older than 30 days, in bounded batches, from
the daily cleanup task.

This module never calls a provider and must not import ``app.ai.providers``.
"""

import logging
import math
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

import anyio
from sqlalchemy import (
    DateTime,
    and_,
    delete,
    func,
    or_,
    select,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Executable

from app.ai import alerts, breaker, entitlements
from app.ai.entitlements import Tier
from app.ai.settings import ZERO_RESULT_ACTION_THROTTLE, AISettings
from app.models.ai_enhanced_zero_result import AIEnhancedZeroResult
from app.models.ai_quota import WINDOW_DAY, WINDOW_MONTH, AIInflight, AIQuota
from app.models.user import User

logger = logging.getLogger(__name__)

# The ai_quota counter key for every scan, standard or enhanced: one shared
# daily and monthly allowance per user (OD-1). Not a gateway feature name.
SCAN_QUOTA_FEATURE = "scan"
# The ai_quota counter key for the Premium enhanced monthly allowance (#533),
# reserved on top of the shared SCAN_QUOTA_FEATURE units.
ENHANCED_QUOTA_FEATURE = "scan.enhanced"
# The Sentry tag for the zero-result warning (the gateway feature name).
ENHANCED_FEATURE_TAG = "scan.enhanced"

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
ZERO_RESULT_RETENTION_DAYS = 30


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


class CostCeilingReached(QuotaRefusal):
    """Today's AI spend is at the Free daily ceiling (503 ``ai_budget_reached``)."""


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


class EnhancedRequiresPremium(QuotaRefusal):
    """Enhanced scans are Premium-only (403 ``enhanced_requires_premium``)."""


class EnhancedThrottled(QuotaRefusal):
    """Too many uncharged zero-result enhanced scans in the window, with
    action ``throttle`` (429 ``enhanced_throttled``)."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("enhanced_throttled")
        self.retry_after = retry_after


class EnhancedLimitReached(QuotaRefusal):
    """The Premium enhanced monthly allowance is used (402
    ``enhanced_limit_reached``)."""

    def __init__(self, limit: int, used: int, period_end: date) -> None:
        super().__init__("enhanced_limit_reached")
        self.limit = limit
        self.used = used
        self.period_end = period_end


@dataclass(frozen=True)
class ZeroResult:
    """Passed to :func:`release` when an enhanced scan's provider answered but
    returned no books, i.e. no candidates (#533). ``ledger_id`` is the usage-ledger
    ``call_id`` (None when the settle has no ledger row)."""

    ledger_id: uuid.UUID | None = None


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
    # The cost estimate added to ai_spend_daily for ``day`` (#531), and the
    # Free ceiling read for this request (for the alert percentages).
    est_cost_micros: int = 0
    ceiling_micros: int = 0
    # Enhanced scans (#533): whether a ENHANCED_QUOTA_FEATURE month unit was
    # taken, the allowance read for this request, and the zero-result
    # settings read for this request (one view of ai_settings per request).
    enhanced: bool = False
    enhanced_allowance: int = 0
    zero_result_threshold: int = 0
    zero_result_window_hours: int = 24
    zero_result_action: str = "charge"


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
# Zero-result enhanced scans (#533)
# ---------------------------------------------------------------------------


def _in_window(user_id: uuid.UUID, window_hours: int):
    # The window is sliding and measured on the database clock, the same
    # clock that stamps the rows (record_zero_result).
    return and_(
        AIEnhancedZeroResult.user_id == user_id,
        AIEnhancedZeroResult.created_at
        > _clock_timestamp() - timedelta(hours=window_hours),
    )


async def zero_result_count(
    db: AsyncSession, user_id: uuid.UUID, window_hours: int
) -> int:
    """The user's uncharged zero-result enhanced scans inside the window (no
    commit)."""
    result = await db.execute(
        select(func.count())
        .select_from(AIEnhancedZeroResult)
        .where(_in_window(user_id, window_hours))
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
    now = (now or utc_now()).astimezone(UTC)
    return max(1, math.ceil((leaves_at - now).total_seconds()))


async def zero_result_throttle(
    db: AsyncSession, user_id: uuid.UUID, threshold: int, window_hours: int
) -> int | None:
    """``None`` while the user's in-window count is below ``threshold``;
    otherwise the Retry-After seconds: until the row whose leaving brings the
    count back below the threshold (the ``count - threshold + 1``-th oldest)
    leaves the window. Ends its read transaction."""
    try:
        count = await zero_result_count(db, user_id, window_hours)
        if count < threshold:
            return None
        leaves_at = None
        if count:
            result = await db.execute(
                select(AIEnhancedZeroResult.created_at)
                .where(_in_window(user_id, window_hours))
                .order_by(AIEnhancedZeroResult.created_at)
                .offset(count - threshold)
                .limit(1)
            )
            oldest = result.scalar_one_or_none()
            if oldest is not None:
                leaves_at = oldest + timedelta(hours=window_hours)
        return retry_after_seconds(leaves_at, window_hours)
    finally:
        # Read only; do not leave the connection idle in a transaction.
        await db.commit()


def zero_result_charged(count: int, threshold: int) -> bool:
    """The charge decision for a zero-result enhanced scan, given the user's
    in-window count before it: charged at or above the threshold (threshold 0
    charges every one)."""
    return count >= threshold


@dataclass(frozen=True)
class ZeroResultRecorded:
    """An uncharged zero-result scan was recorded: the user's new in-window
    count, and whether this row claimed the threshold warning."""

    count: int
    warn: bool = False


async def warned_in_window(
    db: AsyncSession, user_id: uuid.UUID, window_hours: int
) -> bool:
    """True when a row of the user that sent the warning is inside the
    window (no commit)."""
    result = await db.execute(
        select(AIEnhancedZeroResult.id)
        .where(_in_window(user_id, window_hours), AIEnhancedZeroResult.warned)
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def record_zero_result(
    db: AsyncSession, reservation: "Reservation", ledger_id: uuid.UUID | None
) -> ZeroResultRecorded | None:
    """The charge decision, inside the caller's settle transaction (no
    commit). Returns the user's new in-window count when the scan is not
    charged (the enhanced month unit is given back and one row written), or
    ``None`` when it is charged (nothing written).

    The warning claim: when the new row brings the count to the threshold
    and no row of the user with ``warned = true`` is inside the window, the
    new row is written with ``warned = true`` and ``warn`` is returned True.
    The claim commits with the row, so at most one warning per user per
    window; the caller sends it after the commit.

    Runs in a SAVEPOINT: if it fails, only it is rolled back (the unit stays
    charged, no row) and the settle still commits the usage row, the cost and
    the lease delete.
    """
    try:
        async with db.begin_nested():
            count = await zero_result_count(
                db, reservation.user_id, reservation.zero_result_window_hours
            )
            if zero_result_charged(count, reservation.zero_result_threshold):
                return None
            await db.execute(
                update(AIQuota)
                .where(
                    AIQuota.user_id == reservation.user_id,
                    AIQuota.feature == ENHANCED_QUOTA_FEATURE,
                    AIQuota.window_kind == WINDOW_MONTH,
                    AIQuota.window_start == reservation.month,
                    AIQuota.units_used > 0,
                )
                .values(units_used=AIQuota.units_used - 1)
            )
            new_count = count + 1
            warn = (
                new_count == reservation.zero_result_threshold
                and not await warned_in_window(
                    db, reservation.user_id, reservation.zero_result_window_hours
                )
            )
            await db.execute(
                insert(AIEnhancedZeroResult).values(
                    id=uuid.uuid4(),
                    user_id=reservation.user_id,
                    created_at=_clock_timestamp(),
                    ledger_id=ledger_id,
                    warned=warn,
                )
            )
            return ZeroResultRecorded(new_count, warn)
    except Exception:
        logger.exception(
            "ai_quota: zero-result record failed; the enhanced scan stays charged"
        )
        return None


def send_zero_result_warning(
    *, count: int, threshold: int, window_hours: int, action: str
) -> None:
    """One Sentry warning: a user's uncharged zero-result enhanced scans
    reached the threshold. No user id, image, hash or content: the event is
    captured in emptied, stripped scopes (:mod:`app.ai.alerts`). Never
    raises."""
    try:
        alerts.capture_service_warning(
            "ai-enhanced: zero-result enhanced scans reached the threshold",
            fingerprint=["ai-enhanced-zero-results", action],
            tags={"feature": ENHANCED_FEATURE_TAG, "action": action},
            extras={
                "count": count,
                "threshold": threshold,
                "window_hours": window_hours,
            },
        )
        logger.warning(
            "ai_quota: zero-result enhanced scans reached the threshold "
            "(count=%d, window=%dh, action=%s)",
            count,
            window_hours,
            action,
        )
    except Exception:  # a warning must never fail a request
        logger.exception("ai_quota: could not send the zero-result warning")


# ---------------------------------------------------------------------------
# Give-backs: never raise, bounded, shielded from cancellation
# ---------------------------------------------------------------------------


# What _give_back reports: the op succeeded, failed twice with errors, or was
# cut off by CLEANUP_TIMEOUT_SECONDS.
GIVE_BACK_DONE = "done"
GIVE_BACK_FAILED = "failed"
GIVE_BACK_TIMED_OUT = "timed_out"


async def _give_back(
    db: AsyncSession,
    what: str,
    op: Callable[[], Awaitable[object]],
    *,
    attempts: int = 2,
) -> str:
    """Run a give-back so that it cannot mask the original outcome.

    Shielded so it still runs while the request is being cancelled (anyio
    cancellation, as on the request path; a native ``asyncio`` task cancel,
    e.g. at server shutdown, cuts through the shield), and bounded by ``CLEANUP_TIMEOUT_SECONDS``. A failure is logged and retried
    once after a rollback (the session may be in a failed transaction after
    the error being handled); a second failure is logged and swallowed: the
    lease expiry is the backstop. ``attempts=1`` is for an operation that is
    not safe to repeat: if it raised after its commit (an acknowledgement
    lost), a retry would apply it twice.

    If the bound cuts a query off mid-execute, SQLAlchemy invalidates the
    session's connection and every later query on the request session would
    raise ``PendingRollbackError``. So after a timeout the session is rolled
    back (also shielded and bounded; a failure there is logged and swallowed).

    Returns :data:`GIVE_BACK_DONE`, :data:`GIVE_BACK_FAILED` (both attempts
    raised) or :data:`GIVE_BACK_TIMED_OUT` (the bound cut it off).
    """
    with anyio.move_on_after(CLEANUP_TIMEOUT_SECONDS, shield=True) as scope:
        for attempt in range(1, attempts + 1):
            try:
                await op()
                return GIVE_BACK_DONE
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
        return GIVE_BACK_TIMED_OUT
    return GIVE_BACK_FAILED


async def release(
    db: AsyncSession,
    reservation: Reservation,
    ledger_row: Executable | None = None,
    settlement: breaker.Settlement | None = None,
    zero_result: ZeroResult | None = None,
) -> None:
    """Release the lease held by ``reservation`` (settle). Never raises.

    ``zero_result`` (#533, enhanced reservations only): the scan found no
    books, so the charge decision (:func:`record_zero_result`) runs in the
    same transaction as the cost settle and under the same gate, and a
    warning is sent after the commit when the count reaches the threshold.

    ``ledger_row`` (#530) is the usage-ledger insert for the provider call
    (``app.ai.ledger.usage_insert``). It is executed on the same session just
    before the lease delete, so :func:`release_lease`'s commit writes both:
    the ledger row and the settle land in one transaction. The pair is
    bounded, shielded and retried like any give-back. If both attempts fail
    with an error (for example the row itself is rejected by a CHECK or FK),
    the ledger row is dropped and the lease is released on its own, so a
    ledger problem can never keep the lease held or mask the scan's outcome;
    the failure is logged (no content). If the attempt was cut off by the
    bound, the database is slow rather than the row bad: no further attempt
    is made (the settle stays within one bound) and the lease expiry is the
    backstop.

    The cost settle (#531, :func:`app.ai.breaker.settle`) is executed in the
    same transaction: the reservation's estimate leaves ``reserved`` and
    ``settlement.cost_micros`` (0 without a settlement: the estimate is
    given back) is added to ``settled``. It also runs in the lease-only
    fallback, so a dropped ledger row never leaves the estimate reserved.
    After the commit, any spend alert threshold the settle crossed is claimed
    and sent (:func:`_alert`).

    The ledger insert is idempotent (``ON CONFLICT (call_id) DO NOTHING
    RETURNING call_id``), so a retry after a commit whose acknowledgement was
    lost adds no second row; when it returns no row the cost settle is
    skipped too, since it was committed with that row (#531). Without a
    ledger row (refusals in :func:`reserve`, and the lease-only fallback) the
    lease is the proof instead: it is deleted in the same commit as the
    spend, so a retry deletes the lease first and settles the spend only if
    it was still there. If the lease was lost to another request for some
    other reason, the retry leaves the estimate reserved until the day ends,
    which can only make the breaker refuse sooner.
    """
    est = reservation.est_cost_micros
    cost = settlement.cost_micros if settlement is not None else 0
    settled: list[tuple[int, int, int] | None] = []
    recorded: list[ZeroResultRecorded] = []
    attempts = 0
    decide = zero_result is not None and reservation.enhanced

    async def settle_spend() -> None:
        settled.clear()
        recorded.clear()
        if est or cost:
            settled.append(await breaker.settle(db, reservation.day, est, cost))
        if decide:
            count = await record_zero_result(db, reservation, zero_result.ledger_id)
            if count is not None:
                recorded.append(count)

    async def settle_after_lease() -> None:
        # The lease delete commits on its own here, then the spend. If the
        # spend then fails, the estimate stays reserved (fails safe).
        settled.clear()
        recorded.clear()
        if await release_lease(db, reservation.user_id, reservation.lease_id):
            await settle_spend()
            await db.commit()

    async def settle() -> None:
        nonlocal attempts
        attempts += 1
        if ledger_row is None and attempts > 1:
            # A retry: the first attempt may have committed (its
            # acknowledgement lost) with nothing but the lease to show it.
            await settle_after_lease()
            return
        inserted = True
        if ledger_row is not None:
            result = await db.execute(ledger_row)
            if est or cost or decide:
                # ON CONFLICT (call_id) DO NOTHING RETURNING call_id: no row
                # back means an earlier attempt already committed this call's
                # row, and with it the spend settle and the lease delete (a
                # commit whose acknowledgement was lost). Do not add it again.
                inserted = result.scalar_one_or_none() is not None
        if inserted:
            await settle_spend()
        else:
            settled.clear()
            recorded.clear()
        await release_lease(db, reservation.user_id, reservation.lease_id)

    if ledger_row is None:
        outcome = await _give_back(db, "lease release", settle)
    else:
        outcome = await _give_back(db, "lease release with usage row", settle)
        if outcome == GIVE_BACK_TIMED_OUT:
            logger.error(
                "ai_quota: settle timed out; the lease is left to expire, "
                "and the usage row may be missing"
            )
            return
        if outcome == GIVE_BACK_FAILED:
            logger.error("ai_quota: usage row dropped; releasing the lease without it")
            outcome = await _give_back(db, "lease release", settle_after_lease)
    if outcome == GIVE_BACK_DONE and settlement is not None and settled:
        await _alert(db, reservation, settlement, settled[0])
    if outcome == GIVE_BACK_DONE and recorded and recorded[0].warn:
        # The row that claimed the warning (warned = true) is committed, so
        # it is sent at most once per user per window.
        send_zero_result_warning(
            count=recorded[0].count,
            threshold=reservation.zero_result_threshold,
            window_hours=reservation.zero_result_window_hours,
            action=reservation.zero_result_action,
        )


async def _alert(
    db: AsyncSession,
    reservation: Reservation,
    settlement: breaker.Settlement,
    totals: tuple[int, int, int] | None,
) -> None:
    """Claim and send each spend alert this settle crossed (#531). Never
    raises; bounded and shielded like a give-back. A threshold already sent
    today is skipped without a query."""
    if totals is None:
        return
    settled_micros, reserved_micros, mask = totals
    pending = breaker.pending_alerts(settled_micros, reservation.ceiling_micros, mask)
    if not pending:
        return
    won: list[int] = []

    async def claim() -> None:
        won[:] = await breaker.claim_alerts(db, reservation.day, pending)

    if await _give_back(db, "spend alert claim", claim) != GIVE_BACK_DONE:
        return
    for pct in won:
        breaker.send_alert(
            day=reservation.day,
            pct=pct,
            settled_micros=settled_micros,
            reserved_micros=reserved_micros,
            ceiling_micros=reservation.ceiling_micros,
            settlement=settlement,
        )


async def _refund_window(
    db: AsyncSession,
    reservation: Reservation,
    window_kind: str,
    start: date,
) -> None:
    await _give_back(
        db,
        f"{window_kind} refund",
        lambda: refund_unit(
            db, reservation.user_id, reservation.feature, window_kind, start
        ),
    )


async def _refund_enhanced(db: AsyncSession, reservation: Reservation) -> None:
    """Give back the enhanced month unit (#533). Attempted once only:
    ``refund_unit`` has nothing to show whether a failed attempt committed (an
    acknowledgement lost), and a repeat would take a second unit off. A
    failure leaves the user charged (fails safe)."""
    await _give_back(
        db,
        "enhanced month refund",
        lambda: refund_unit(
            db,
            reservation.user_id,
            ENHANCED_QUOTA_FEATURE,
            WINDOW_MONTH,
            reservation.month,
        ),
        attempts=1,
    )


async def refund(db: AsyncSession, reservation: Reservation) -> None:
    """Give back the daily and monthly units, and the enhanced month unit of
    an enhanced reservation (provider unavailable or never reached). Never
    raises; does not release the lease (call :func:`release`)."""
    await _refund_window(db, reservation, WINDOW_DAY, reservation.day)
    await _refund_window(db, reservation, WINDOW_MONTH, reservation.month)
    if reservation.enhanced:
        await _refund_enhanced(db, reservation)


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
    est_cost_micros: int,
    now: datetime | None = None,
    enhanced: bool = False,
) -> Reservation:
    """Check the allowances and take the cost estimate, the lease, a daily
    and a monthly unit, and for ``enhanced`` an enhanced month unit (#533).

    ``est_cost_micros`` is the gateway feature's
    ``FeatureSpec.est_cost_ceiling_micros`` (server-side registry value).

    Raises a :class:`QuotaRefusal` (after giving back whatever was taken) when
    the call must not go ahead. Any other exception also gives back what was
    taken before it propagates. On success the caller owns the reservation and
    must :func:`release` it after the provider call.
    """
    tier = await entitlements.tier_for(db, user)
    if enhanced and tier is not Tier.PREMIUM:
        # Before anything is taken (#533).
        raise EnhancedRequiresPremium("enhanced_requires_premium")
    allowance = allowance_for(ai, tier)
    today = utc_today(now)
    month = month_start(today)

    if await global_units_today(db, today) >= ai.global_daily_limit:
        raise GlobalCapacityReached("scan_capacity")

    # Free pauses at the ceiling; Premium (and only Premium) is not gated by
    # it but still adds its estimate (#544). Any other tier is gated.
    ceiling = ai.free_daily_ceiling_micros
    if not await breaker.reserve(
        db, today, est_cost_micros, ceiling, enforce=tier is not Tier.PREMIUM
    ):
        raise CostCeilingReached("ai_budget_reached")

    async def give_back_cost() -> None:
        # One attempt only: there is no lease or ledger row yet to show
        # whether a failed attempt committed, and a repeated release would
        # give back another call's estimate. A failure leaves the estimate
        # reserved until the day ends (the breaker refuses sooner, not later).
        await _give_back(
            db,
            "cost release",
            lambda: breaker.release(db, today, est_cost_micros),
            attempts=1,
        )

    seconds = lease_seconds(timeout_s)
    try:
        lease_id = await acquire_lease(db, user.id, seconds)
    except BaseException:
        await give_back_cost()
        raise
    if lease_id is None:
        await give_back_cost()
        raise ScanInProgress("scan_in_progress")
    reservation = Reservation(
        user_id=user.id,
        feature=feature,
        lease_id=lease_id,
        day=today,
        month=month,
        tier=tier,
        allowance=allowance,
        est_cost_micros=est_cost_micros,
        ceiling_micros=ceiling,
        enhanced=enhanced,
        enhanced_allowance=ai.enhanced_monthly_allowance(tier.value),
        zero_result_threshold=ai.zero_result_threshold,
        zero_result_window_hours=ai.zero_result_window_hours,
        zero_result_action=ai.zero_result_action,
    )

    daily_taken = monthly_taken = enhanced_taken = False
    try:
        if enhanced and reservation.zero_result_action == ZERO_RESULT_ACTION_THROTTLE:
            retry_after = await zero_result_throttle(
                db,
                user.id,
                reservation.zero_result_threshold,
                reservation.zero_result_window_hours,
            )
            if retry_after is not None:
                raise EnhancedThrottled(retry_after)
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
        if enhanced:
            if (
                await reserve_unit(
                    db,
                    user.id,
                    ENHANCED_QUOTA_FEATURE,
                    WINDOW_MONTH,
                    month,
                    reservation.enhanced_allowance,
                )
                is None
            ):
                used = await units_used(
                    db, user.id, ENHANCED_QUOTA_FEATURE, WINDOW_MONTH, month
                )
                raise EnhancedLimitReached(
                    limit=reservation.enhanced_allowance,
                    used=used,
                    period_end=period_end(today),
                )
            enhanced_taken = True
        # The upserts above are unbounded, and the lease aged while they ran.
        # Restart its clock so the whole timeout_s + 30 s covers the provider
        # call, and stop if a stall let it expire and another request take it.
        if not await renew_lease(db, user.id, lease_id, seconds):
            raise ScanInProgress("scan_in_progress")
    except BaseException:
        # Includes CancelledError: give back what was taken, then propagate.
        if enhanced_taken:
            await _refund_enhanced(db, reservation)
        if monthly_taken:
            await _refund_window(db, reservation, WINDOW_MONTH, month)
        if daily_taken:
            await _refund_window(db, reservation, WINDOW_DAY, today)
        # Releases the lease and gives back the cost estimate.
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
    # Enhanced scans this UTC month and the enhanced allowance (0 for every
    # tier but Premium, #533).
    enhanced_used_month: int = 0
    enhanced_monthly_limit: int = 0


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
        select(AIQuota.feature, AIQuota.window_kind, AIQuota.units_used).where(
            AIQuota.user_id == user.id,
            or_(
                and_(
                    AIQuota.feature == feature,
                    AIQuota.window_kind == WINDOW_DAY,
                    AIQuota.window_start == today,
                ),
                and_(
                    AIQuota.feature.in_((feature, ENHANCED_QUOTA_FEATURE)),
                    AIQuota.window_kind == WINDOW_MONTH,
                    AIQuota.window_start == month,
                ),
            ),
        )
    )
    used = {(feat, kind): int(n) for feat, kind, n in result.all()}
    return Usage(
        used_today=used.get((feature, WINDOW_DAY), 0),
        daily_limit=allowance.daily,
        used_month=used.get((feature, WINDOW_MONTH), 0),
        monthly_limit=allowance.monthly,
        period_end=period_end(today),
        enhanced_used_month=used.get((ENHANCED_QUOTA_FEATURE, WINDOW_MONTH), 0),
        enhanced_monthly_limit=ai.enhanced_monthly_allowance(tier.value),
    )


async def enhanced_remaining(db: AsyncSession, reservation: Reservation) -> int | None:
    """The user's remaining enhanced allowance this month after the scan
    (#533): ``None`` for every tier but Premium. Ends its read
    transaction."""
    if reservation.tier is not Tier.PREMIUM:
        return None
    try:
        used = await units_used(
            db,
            reservation.user_id,
            ENHANCED_QUOTA_FEATURE,
            WINDOW_MONTH,
            reservation.month,
        )
    finally:
        await db.commit()
    return max(0, reservation.enhanced_allowance - used)


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


async def purge_old_zero_results(db: AsyncSession, now: datetime | None = None) -> int:
    """Delete ``ai_enhanced_zero_results`` rows older than 30 days in bounded
    batches; return the count."""
    cutoff = (now or utc_now()) - timedelta(days=ZERO_RESULT_RETENTION_DAYS)
    total = 0
    for _ in range(PURGE_MAX_BATCHES):
        batch = (
            select(AIEnhancedZeroResult.id)
            .where(AIEnhancedZeroResult.created_at < cutoff)
            .limit(PURGE_BATCH_SIZE)
        )
        result = await db.execute(
            delete(AIEnhancedZeroResult).where(AIEnhancedZeroResult.id.in_(batch))
        )
        await db.commit()
        deleted = result.rowcount or 0
        total += deleted
        if deleted < PURGE_BATCH_SIZE:
            break
    return total

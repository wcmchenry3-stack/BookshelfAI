"""Per-user AI allowances: reserve before the provider, settle or refund after.

Every paid scan goes through :func:`reserve` before the provider is called.
In order (#528):

1. **Entitlement.** The tier comes from ``entitlements.tier_for(db, user)``
   (server-side only); the allowances come from ``ai_settings``
   (``quota.<tier>.found_daily`` and ``quota.<tier>.no_match_daily``, #577).
   Nothing in the request can change either.
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
   cannot delete the new holder's lease. After the reservation below the
   lease is renewed (:func:`renew_lease`), so the provider call gets the full
   lease; if a stall let another request take it over, the reservation is
   given back and :class:`ScanInProgress` raised.
5. **Daily reservation** (table ``ai_quota``). The same exact conditional
   upsert as #509: ``units_used = units_used + 1 WHERE units_used < limit``.
   Postgres serialises upserts on the same key, so no more than ``limit``
   reservations can succeed (:class:`DailyLimitReached`). A limit of 0 refuses
   without writing. Standard and enhanced scans share one allowance (OD-1):
   both reserve under :data:`SCAN_QUOTA_FEATURE`. The ``feature`` column stays
   so later AI features can have their own allowance.

**No monthly limit (#582).** There is no monthly allowance for any tier, and
no Premium enhanced monthly allowance: nothing here takes, checks or gives
back a month-window unit, and no scan is refused for a monthly reason. What
bounds a month is the daily allowance of each user (at most 31 x the daily
limit), the service-wide ``scan.global_daily_limit``, the daily spend ceiling
(``breaker.free_daily_ceiling_micros``) and, outside the code, the monthly
spend limit in the provider console (#542). Spend and call counts per month
stay derivable from the usage ledger (``ai_usage`` / ``ai_usage_daily``,
``app/ai/ledger.py``): tracking, not a limit. Month rows (``window_kind =
'month'``) written before #582 are no longer read or written; the retention
purge below removes them as they age past 13 months (kept so a rollback to
the previous release finds its counters intact).

**Unit flag (#590).** The daily unit is taken by :func:`take_unit`: the
``ai_quota`` increment and a flag on the request's own lease row
(``ai_inflight.daily_taken``, matched on ``lease_id``) commit together; if the
lease is no longer ours nothing is taken (:class:`ScanInProgress`). So after a
commit whose acknowledgement was lost, the flag shows whether the unit
landed. Every give-back of the unit (:func:`refund`, and the ``except`` in
:func:`reserve`, which covers a lost acknowledgement on a take) goes through
:func:`give_back_unit`: it clears the flag and takes the unit off in one
commit, only if the flag was set, so a retry gives nothing back twice; the
give-backs are therefore retried like the others (#567 had made them
one-shot when nothing gated them). The settle in :func:`release` needs no
flag: it deletes the lease row, flag and all. If another request took over
the expired lease, the takeover resets the flags (they belong to the new
lease): a unit this request knows it took (its take was acknowledged) is then
given back once without the gate, on the first attempt only; a unit whose
take was never acknowledged stays charged (fails toward charging). The
``monthly_taken`` and ``enhanced_taken`` columns no longer exist (dropped by
migration 0018, #594).

**Found and no-match counters (#577).** Each user has two daily counters:
``found`` (:data:`SCAN_QUOTA_FEATURE`, the daily unit above: every scan takes
one before the provider, so a scan is refused with 429 ``daily_scan_limit``
when found is full, even if no-match has room, since the next photo may find
books) and ``no_match`` (:data:`SCAN_NO_MATCH_QUOTA_FEATURE`). When a
**standard** scan's provider answered properly (outcome ``ok``) with no
candidates, the caller passes a :class:`NoMatch` to :func:`release`, and in
the settle transaction (the same gate as the cost settle and the #566
zero-result record: the ledger insert, or the lease, so a retried settle
never moves twice) :func:`move_to_no_match` moves the unit: one no-match unit
is taken (``units_used < no_match ceiling``, the same exact conditional
upsert) and the found unit given back, both under the lock of the request's
own lease row and only while its ``daily_taken`` flag is set (cleared in the
same transaction). If no-match is full, the found unit stays charged
(``counted``). The move runs in a SAVEPOINT: a failure there keeps the scan
charged and never breaks the settle or the response. Enhanced scans never
move (the #533 zero-result rule stays for them), nor do malformed (already
refunded), refused or truncated replies. The service-wide count sums every
day row, so it still counts every provider call, found or no-match. So a
user's provider calls per day never exceed found + no-match ceilings. The
move that brings no-match to its ceiling also logs a line and, after the
commit, sends one Sentry warning (:func:`send_no_match_ceiling_warning`):
counts only rise within a day, so that is at most once per user per day
(per ceiling value: raising the ceiling mid-day lets the new one be reached
once more). After a lost commit acknowledgement the retry does not move and
sends nothing (fewer alerts, never more).

**/scan requests per user per day (#586).** Before any of the steps above,
and before the scan-record lookup, ``/scan`` calls
:func:`count_scan_request`: one more request under
:data:`SCAN_REQUESTS_QUOTA_FEATURE` for the user's UTC day, with the same
exact conditional upsert (``units_used < scan.requests_per_user_daily``),
or :class:`ScanRequestsDailyLimitReached` (429
``scan_requests_daily_limit``, Retry-After to UTC midnight) with nothing
written. It counts every request past authentication and the kill
switches, new photo or repeat, allowed or refused later, and nothing is
ever given back. It is the abuse control for repeats, which are free
(decision #573 point 3). It counts requests, not AI calls, so
:func:`global_units_today` leaves the key out
(:data:`NON_AI_QUOTA_FEATURES`); the rows are day rows, purged with the
others.

**Enhanced scans (#533).** ``reserve(..., enhanced=True)`` adds two steps:

- right after the tier is resolved, a non-Premium tier is refused
  (:class:`EnhancedRequiresPremium`, 403 ``enhanced_requires_premium``) before
  anything is taken (no cost estimate, lease or unit);
- after the lease, with ``scan.enhanced.zero_result_action`` = ``throttle``,
  the user's zero-result enhanced scans recorded inside the sliding window
  (``ai_enhanced_zero_results``) are counted; at or above
  ``scan.enhanced.zero_result_threshold`` the scan is refused
  (:class:`EnhancedThrottled`, 429 ``enhanced_throttled``) with a Retry-After
  of the seconds until enough rows leave the window. Counting under the lease
  serialises it with the user's own settle.

An enhanced scan takes the shared daily unit and nothing else (#582: the
Premium enhanced monthly allowance is gone; a per-model daily Sonnet counter
comes with #583). The zero-result rule stays as the abuse signal: when the
provider answered but returned no books (no candidates), the caller passes a
:class:`ZeroResult` to :func:`release`, and in the settle transaction (gated
exactly like the cost settle, so a retried settle cannot apply it twice) the
user's in-window count is read: below the threshold one
``ai_enhanced_zero_results`` row is written; at or above it nothing is
written. The daily unit stays charged either way (the provider answered).
The write runs in a SAVEPOINT, so a failure there never drops the usage row.
When a row brings the count to the threshold and no row of the user with
``warned = true`` is inside the window, that row is written with ``warned =
true`` (the claim commits with it) and one Sentry warning is sent after the
commit (:func:`send_zero_result_warning`): at most one per user per window.
Threshold 0: with ``charge`` no rows and no warning; with ``throttle`` every
enhanced scan is refused. A repeat served from a scan record (#585) makes no
provider call and must not pass a :class:`ZeroResult`.

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
:func:`refund`, which gives back the daily unit at most once, never below
zero. Any other outcome (success, an empty result, another error, a cancelled
request) keeps the reservation: the provider may have been paid.

Windows are UTC: the day window starts at UTC midnight.

Retention: :func:`purge_old_quota` deletes day rows older than 90 days and
(legacy, see above) month rows older than 13 months, and
:func:`purge_old_zero_results` deletes ``ai_enhanced_zero_results`` rows older
than 30 days, in bounded batches, from the daily cleanup task.

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
# daily allowance per user (OD-1). Not a gateway feature name.
SCAN_QUOTA_FEATURE = "scan"
# The ai_quota counter key of no-match scans (#577): standard scans whose
# provider answered properly but found no book. The found counter is
# SCAN_QUOTA_FEATURE.
SCAN_NO_MATCH_QUOTA_FEATURE = "scan.no_match"
# The ai_quota counter key of /scan requests (#586): every authenticated
# /scan request past the kill switches, new photo or repeat, refused or not.
# A request count, not an AI call, so the service-wide count
# (global_units_today) leaves it out (NON_AI_QUOTA_FEATURES).
SCAN_REQUESTS_QUOTA_FEATURE = "scan.requests"
# ai_quota keys that count something other than AI calls: never summed into
# the global daily cap.
NON_AI_QUOTA_FEATURES = (SCAN_REQUESTS_QUOTA_FEATURE,)
# What /scan says about a no-match (ScanResponse.no_match, #577).
NO_MATCH_FREE = "free"
NO_MATCH_COUNTED = "counted"
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

# The ai_inflight flag (#590): whether the lease holder took its daily unit.
FLAG_DAILY = "daily_taken"
UNIT_FLAGS = (FLAG_DAILY,)
# What give_back_unit reports.
UNIT_GIVEN_BACK = "given_back"
UNIT_NOT_TAKEN = "not_taken"
UNIT_LEASE_LOST = "lease_lost"


# ---------------------------------------------------------------------------
# Windows (UTC)
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_today(now: datetime | None = None) -> date:
    return (now or utc_now()).astimezone(UTC).date()


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
    """The tier's per-user allowances. Daily only: there is no monthly
    allowance (#582). ``daily`` is the found counter's ceiling (#577: every
    scan reserves a found unit); ``no_match`` the no-match ceiling."""

    daily: int
    no_match: int = 0


def allowance_for(ai: AISettings, tier: Tier) -> Allowance:
    """The tier's allowances from ``ai_settings`` (validated, bounded)."""
    return Allowance(
        daily=ai.daily_allowance(tier.value),
        no_match=ai.no_match_allowance(tier.value),
    )


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


class ScanRequestsDailyLimitReached(QuotaRefusal):
    """The user has made today's allowed number of /scan requests (#586,
    429 ``scan_requests_daily_limit``); resets at UTC midnight."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("scan_requests_daily_limit")
        self.retry_after = retry_after


class EnhancedRequiresPremium(QuotaRefusal):
    """Enhanced scans are Premium-only (403 ``enhanced_requires_premium``)."""


class EnhancedThrottled(QuotaRefusal):
    """Too many uncharged zero-result enhanced scans in the window, with
    action ``throttle`` (429 ``enhanced_throttled``)."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("enhanced_throttled")
        self.retry_after = retry_after


@dataclass(frozen=True)
class ZeroResult:
    """Passed to :func:`release` when an enhanced scan's provider answered but
    returned no books, i.e. no candidates (#533). ``ledger_id`` is the usage-ledger
    ``call_id`` (None when the settle has no ledger row)."""

    ledger_id: uuid.UUID | None = None


@dataclass(frozen=True)
class NoMatch:
    """Passed to :func:`release` when a standard scan's provider answered
    properly (outcome ``ok``) with no candidates (#577): the settle moves the
    found unit to the no-match counter when it has room."""


@dataclass(frozen=True)
class NoMatchMoved:
    """What the no-match decision did: ``outcome`` is :data:`NO_MATCH_FREE`
    (moved: no found unit used) or :data:`NO_MATCH_COUNTED` (no-match was
    full: the found unit stays charged); ``count`` is the user's no-match
    count after it; ``warn`` whether this move brought it to the ceiling
    (the ceiling warning's claim)."""

    outcome: str
    count: int
    warn: bool = False


@dataclass(frozen=True)
class Reservation:
    """What :func:`reserve` took; hand it to :func:`refund` and :func:`release`."""

    user_id: uuid.UUID
    feature: str
    lease_id: uuid.UUID
    day: date
    tier: Tier
    allowance: Allowance
    # The cost estimate added to ai_spend_daily for ``day`` (#531), and the
    # Free ceiling read for this request (for the alert percentages).
    est_cost_micros: int = 0
    ceiling_micros: int = 0
    # Enhanced scans (#533): whether this is one, and the zero-result
    # settings read for this request (one view of ai_settings per request).
    enhanced: bool = False
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
            # The old holder's unit flags (#590) do not carry over to the
            # new lease: the new holder has taken nothing yet.
            **{flag: False for flag in UNIT_FLAGS},
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


def _unit_upsert(
    user_id: uuid.UUID,
    feature: str,
    window_kind: str,
    window_start: date,
    limit: int,
):
    """``units_used + 1`` only while under ``limit`` (#509), returning the new
    count, or no row when the limit is reached."""
    return (
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


async def reserve_unit(
    db: AsyncSession,
    user_id: uuid.UUID,
    feature: str,
    window_kind: str,
    window_start: date,
    limit: int,
) -> int | None:
    """Reserve one unit in the window if under ``limit``; return the new count,
    or ``None`` when the limit is reached (nothing changes then). Not tied to
    a lease: :func:`reserve` uses :func:`take_unit`."""
    if limit <= 0:
        # The insert path would create the row at 1; a zero allowance must
        # refuse without writing.
        return None
    result = await db.execute(
        _unit_upsert(user_id, feature, window_kind, window_start, limit)
    )
    used = result.scalar_one_or_none()
    await db.commit()
    return int(used) if used is not None else None


def _flag_column(flag: str):
    if flag not in UNIT_FLAGS:
        raise ValueError(f"unknown unit flag: {flag}")
    return getattr(AIInflight, flag)


def _held(user_id: uuid.UUID, lease_id: uuid.UUID):
    return and_(AIInflight.user_id == user_id, AIInflight.lease_id == lease_id)


async def take_unit(
    db: AsyncSession,
    user_id: uuid.UUID,
    lease_id: uuid.UUID,
    flag: str,
    feature: str,
    window_kind: str,
    window_start: date,
    limit: int,
) -> int | None:
    """Reserve one unit under the lease ``lease_id`` (#590): the ``ai_quota``
    increment and the lease row's ``flag`` are written in one transaction, so
    after a commit whose acknowledgement was lost the flag shows whether the
    unit landed. Returns the new count, or ``None`` when the limit is reached
    (nothing changes then). Raises :class:`ScanInProgress` without taking
    anything when ``lease_id`` no longer holds the lease (fails closed)."""
    _flag_column(flag)
    if limit <= 0:
        # Refuses without writing, as reserve_unit.
        return None
    marked = await db.execute(
        update(AIInflight)
        .where(_held(user_id, lease_id))
        .values({flag: True})
        .returning(AIInflight.lease_id)
    )
    if marked.scalar_one_or_none() is None:
        await db.commit()  # nothing was written
        raise ScanInProgress("scan_in_progress")
    result = await db.execute(
        _unit_upsert(user_id, feature, window_kind, window_start, limit)
    )
    used = result.scalar_one_or_none()
    if used is None:
        # Refused: clear the flag again in the same transaction (each unit is
        # taken at most once per lease, so it was clear before). Not a
        # rollback, which would expire the caller's ORM objects.
        await db.execute(
            update(AIInflight).where(_held(user_id, lease_id)).values({flag: False})
        )
        await db.commit()
        return None
    await db.commit()
    return int(used)


async def give_back_unit(
    db: AsyncSession,
    user_id: uuid.UUID,
    lease_id: uuid.UUID,
    flag: str,
    feature: str,
    window_kind: str,
    window_start: date,
) -> str:
    """The gated give-back (#590): clear the lease row's ``flag`` (only while
    ``lease_id`` holds the lease and the flag is set) and, only if it was
    cleared, take one unit off in the same transaction, never below zero. A
    retry after a commit whose acknowledgement was lost finds the flag clear
    and gives nothing back, so a unit comes back at most once.

    Returns :data:`UNIT_GIVEN_BACK`, :data:`UNIT_NOT_TAKEN` (the lease is
    held but the flag is clear: never taken, or already given back) or
    :data:`UNIT_LEASE_LOST` (another request holds the lease now; nothing
    changes)."""
    column = _flag_column(flag)
    cleared = await db.execute(
        update(AIInflight)
        .where(_held(user_id, lease_id), column.is_(True))
        .values({flag: False})
        .returning(AIInflight.lease_id)
    )
    if cleared.scalar_one_or_none() is None:
        held = await db.execute(
            select(AIInflight.lease_id).where(_held(user_id, lease_id))
        )
        still_held = held.scalar_one_or_none() is not None
        await db.commit()
        return UNIT_NOT_TAKEN if still_held else UNIT_LEASE_LOST
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
    return UNIT_GIVEN_BACK


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


def global_units_filter(day: date):
    """The ``ai_quota`` rows the service-wide count sums: ``day``'s day rows
    of every AI-call key, so not the /scan request counter (#586:
    :data:`NON_AI_QUOTA_FEATURES`)."""
    return and_(
        AIQuota.window_kind == WINDOW_DAY,
        AIQuota.window_start == day,
        AIQuota.feature.not_in(NON_AI_QUOTA_FEATURES),
    )


async def global_units_today(db: AsyncSession, day: date) -> int:
    """AI-call units reserved on ``day`` across all users and features
    (found and no-match); /scan request counts are left out (#586)."""
    result = await db.execute(
        select(func.coalesce(func.sum(AIQuota.units_used), 0)).where(
            global_units_filter(day)
        )
    )
    return int(result.scalar_one())


# ---------------------------------------------------------------------------
# /scan requests per user per UTC day (#586)
# ---------------------------------------------------------------------------


async def count_scan_request(
    db: AsyncSession,
    user_id: uuid.UUID,
    ai: AISettings,
    now: datetime | None = None,
) -> int:
    """Count one /scan request for ``user_id`` today (UTC) and return the
    new count, or raise :class:`ScanRequestsDailyLimitReached` (nothing
    written) when ``scan.requests_per_user_daily`` requests were already
    made today.

    The same exact conditional upsert as the daily unit (``units_used + 1
    WHERE units_used < limit``, committed straight away), so parallel
    requests on any number of instances never pass the limit. It counts
    requests, not scans: nothing is ever given back, whatever happens to
    the request afterwards. Not a lease or allowance step and not an AI
    call, so it is kept out of :func:`global_units_today`."""
    now = now or utc_now()
    used = await reserve_unit(
        db,
        user_id,
        SCAN_REQUESTS_QUOTA_FEATURE,
        WINDOW_DAY,
        utc_today(now),
        ai.scan_requests_per_user_daily,
    )
    if used is None:
        raise ScanRequestsDailyLimitReached(retry_after=seconds_until_utc_midnight(now))
    return used


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
    """Whether a zero-result enhanced scan is past the threshold, given the
    user's in-window count before it: at or above the threshold nothing is
    recorded (threshold 0: never recorded). Named for the #533 rule, when
    below the threshold also gave back the enhanced monthly unit (#582
    removed that unit)."""
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


async def hold_lease_lock(
    db: AsyncSession, user_id: uuid.UUID, lease_id: uuid.UUID
) -> bool:
    """Lock the user's lease row if ``lease_id`` still holds it (no commit);
    False when this request no longer holds the lease."""
    result = await db.execute(
        select(AIInflight.lease_id)
        .where(AIInflight.user_id == user_id, AIInflight.lease_id == lease_id)
        .with_for_update()
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
            if not await hold_lease_lock(db, reservation.user_id, reservation.lease_id):
                return None
            count = await zero_result_count(
                db, reservation.user_id, reservation.zero_result_window_hours
            )
            if zero_result_charged(count, reservation.zero_result_threshold):
                return None
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
        logger.exception("ai_quota: zero-result record failed; no row written")
        return None


async def move_to_no_match(
    db: AsyncSession, reservation: "Reservation"
) -> NoMatchMoved | None:
    """The no-match move (#577), inside the caller's settle transaction (no
    commit): take one no-match unit if under the ceiling and give back the
    found unit. Returns what it did, or ``None`` when nothing was decided
    (the request no longer holds its lease, its found unit is not flagged as
    taken, or the move failed): the scan then stays charged.

    Gate: the settle runs this only when it is the attempt that writes the
    call's ledger row (or, without one, the attempt that deletes the lease),
    exactly like :func:`record_zero_result`, so a retry after a lost commit
    acknowledgement never moves twice. Here the request's own lease row is
    locked (``FOR UPDATE``) and must still carry ``daily_taken``; the flag is
    cleared in the same transaction, so nothing can give the found unit back
    again. The lease delete later in the settle releases the lock at commit.

    The no-match take is the exact conditional upsert of the found unit
    (``units_used + 1 WHERE units_used < ceiling``), so concurrent moves
    never pass the ceiling. Ceiling 0: never moves.

    Runs in a SAVEPOINT: on a failure only it is rolled back (logged) and the
    scan stays charged; the settle still commits the usage row, the cost and
    the lease delete.
    """
    unit_flag = _flag_column(FLAG_DAILY)
    try:
        async with db.begin_nested():
            held = await db.execute(
                select(AIInflight.lease_id)
                .where(
                    _held(reservation.user_id, reservation.lease_id),
                    unit_flag.is_(True),
                )
                .with_for_update()
            )
            if held.scalar_one_or_none() is None:
                return None
            ceiling = reservation.allowance.no_match
            taken = None
            if ceiling > 0:
                result = await db.execute(
                    _unit_upsert(
                        reservation.user_id,
                        SCAN_NO_MATCH_QUOTA_FEATURE,
                        WINDOW_DAY,
                        reservation.day,
                        ceiling,
                    )
                )
                taken = result.scalar_one_or_none()
            if taken is None:
                # No-match is full: the found unit stays charged.
                logger.info(
                    "ai_quota: no-match counted as a scan, ceiling reached "
                    "(tier=%s, ceiling=%d)",
                    _tier_tag(reservation.tier),
                    ceiling,
                )
                return NoMatchMoved(NO_MATCH_COUNTED, ceiling)
            await db.execute(
                update(AIQuota)
                .where(
                    AIQuota.user_id == reservation.user_id,
                    AIQuota.feature == reservation.feature,
                    AIQuota.window_kind == WINDOW_DAY,
                    AIQuota.window_start == reservation.day,
                    AIQuota.units_used > 0,
                )
                .values(units_used=AIQuota.units_used - 1)
            )
            await db.execute(
                update(AIInflight)
                .where(_held(reservation.user_id, reservation.lease_id))
                .values({FLAG_DAILY: False})
            )
            count = int(taken)
            warn = count == ceiling
            if warn:
                logger.warning(
                    "ai_quota: a user's no-match scans reached the daily ceiling "
                    "(tier=%s, ceiling=%d)",
                    _tier_tag(reservation.tier),
                    ceiling,
                )
            return NoMatchMoved(NO_MATCH_FREE, count, warn)
    except Exception:
        logger.exception("ai_quota: no-match move failed; the scan stays charged")
        return None


def _tier_tag(tier: object) -> str:
    return tier.value if isinstance(tier, Tier) else "unknown"


def send_no_match_ceiling_warning(*, tier: object, ceiling: int) -> None:
    """One Sentry warning: a user's no-match scans reached the daily ceiling
    (#577). Only the tier and the ceiling: no user id, image, hash or
    content (emptied, stripped scopes, :mod:`app.ai.alerts`). Never
    raises."""
    try:
        alerts.capture_service_warning(
            "ai-quota: a user's no-match scans reached the daily ceiling",
            fingerprint=["ai-no-match-ceiling"],
            tags={"tier": _tier_tag(tier)},
            extras={"ceiling": ceiling},
        )
    except Exception:  # a warning must never fail a request
        logger.exception("ai_quota: could not send the no-match ceiling warning")


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


# What _give_back reports: the op succeeded, raised on every attempt, or was
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

    Returns :data:`GIVE_BACK_DONE`, :data:`GIVE_BACK_FAILED` (every attempt
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
    no_match: NoMatch | None = None,
) -> NoMatchMoved | None:
    """Release the lease held by ``reservation`` (settle). Never raises.

    ``no_match`` (#577, standard reservations only): the provider answered
    with no books, so the no-match move (:func:`move_to_no_match`) runs in
    the same transaction as the cost settle and under the same gate. Returns
    what it decided once the settle committed, else ``None`` (no
    ``no_match``, an enhanced reservation, a retried or failed settle, or a
    lost lease: the scan stays charged). The ceiling warning is sent after
    the commit.

    ``zero_result`` (#533, enhanced reservations only): the scan found no
    books, so the zero-result record (:func:`record_zero_result`) runs in the
    same transaction as the cost settle and under the same gate, and a
    warning is sent after the commit when the count reaches the threshold.

    ``ledger_row`` (#530) is the usage-ledger insert for the provider call
    (``app.ai.ledger.usage_insert``). It is executed on the same session just
    before the lease delete, so :func:`release_lease`'s commit writes both:
    the ledger row and the settle land in one transaction. The pair is
    bounded, shielded and retried like any give-back. If every attempt fails
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
    moved: list[NoMatchMoved] = []
    attempts = 0
    decide = zero_result is not None and reservation.enhanced
    # Enhanced scans never move (#577: the #533 zero-result rule stays).
    move = no_match is not None and not reservation.enhanced

    async def settle_spend() -> None:
        settled.clear()
        recorded.clear()
        moved.clear()
        if est or cost:
            settled.append(await breaker.settle(db, reservation.day, est, cost))
        if decide:
            count = await record_zero_result(db, reservation, zero_result.ledger_id)
            if count is not None:
                recorded.append(count)
        if move:
            decision = await move_to_no_match(db, reservation)
            if decision is not None:
                moved.append(decision)

    async def settle_after_lease() -> None:
        # The lease delete commits on its own here, then the spend. If the
        # spend then fails, the estimate stays reserved (fails safe).
        settled.clear()
        recorded.clear()
        moved.clear()
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
            if est or cost or decide or move:
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
            moved.clear()
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
            return None
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
    if outcome != GIVE_BACK_DONE or not moved:
        return None
    if moved[0].warn:
        # Claimed by the move that brought the count to the ceiling, now
        # committed: at most once per user per day.
        send_no_match_ceiling_warning(
            tier=reservation.tier, ceiling=reservation.allowance.no_match
        )
    return moved[0]


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


@dataclass(frozen=True)
class _Unit:
    """One unit a scan takes: its lease-row flag and its ai_quota key."""

    flag: str
    feature: str
    window_kind: str
    window_start: date
    what: str


def _daily_unit(reservation: Reservation) -> _Unit:
    """The daily unit of ``reservation``: the only unit a scan takes (#582)."""
    return _Unit(
        FLAG_DAILY,
        reservation.feature,
        WINDOW_DAY,
        reservation.day,
        "daily",
    )


async def _return_unit(
    db: AsyncSession, reservation: Reservation, unit: _Unit, *, taken: bool
) -> None:
    """Give back one unit at most once (#590), bounded and retried like any
    give-back (:func:`_give_back`).

    The gate is :func:`give_back_unit`: the flag on the request's lease row
    is cleared in the same commit as the decrement, so a retry after a lost
    acknowledgement finds it clear and gives nothing back, and a unit whose
    take was never acknowledged is given back only if its flag shows it
    landed. That is why the refunds retry again (#567 had made them
    one-shot because nothing gated them).

    ``taken`` means this request knows the unit was taken (its take was
    acknowledged). If another request took over the expired lease, the
    flags belong to the new lease (the takeover resets them), so the gate
    cannot show anything: on the first attempt only, a known-taken unit is
    then given back once without the gate (:func:`refund_unit`, as before
    #590). A later attempt never does that, since the first may have
    committed. A unit whose take was not acknowledged and whose lease was
    taken over stays charged (fails toward charging)."""
    attempts = 0
    last_outcome: str | None = None
    fallback_done = False

    async def op() -> None:
        nonlocal attempts, last_outcome, fallback_done
        attempts += 1
        last_outcome = None
        outcome = await give_back_unit(
            db,
            reservation.user_id,
            reservation.lease_id,
            unit.flag,
            unit.feature,
            unit.window_kind,
            unit.window_start,
        )
        last_outcome = outcome
        if outcome == UNIT_LEASE_LOST and taken and attempts == 1:
            await refund_unit(
                db,
                reservation.user_id,
                unit.feature,
                unit.window_kind,
                unit.window_start,
            )
            fallback_done = True

    result = await _give_back(db, f"{unit.what} refund", op)
    # An accepted exception (#590), tracked: a unit left charged.
    if result != GIVE_BACK_DONE:
        send_unit_kept_warning(unit, UNIT_KEPT_GIVE_BACK_FAILED)
    elif last_outcome == UNIT_LEASE_LOST and not taken:
        send_unit_kept_warning(unit, UNIT_KEPT_LEASE_LOST_UNACKNOWLEDGED)
    elif last_outcome == UNIT_LEASE_LOST and not fallback_done:
        send_unit_kept_warning(unit, UNIT_KEPT_FALLBACK_FAILED)


# Why a unit the request attempted to take was left charged (the fingerprint's
# second element and the ``reason`` tag of the Sentry warning).
UNIT_KEPT_LEASE_LOST_UNACKNOWLEDGED = "lease_lost_unacknowledged"
UNIT_KEPT_FALLBACK_FAILED = "fallback_failed"
UNIT_KEPT_GIVE_BACK_FAILED = "give_back_failed"
_UNIT_TAGS = {
    FLAG_DAILY: "daily",
}


def send_unit_kept_warning(unit: _Unit, reason: str) -> None:
    """One Sentry warning and one log line: a unit this request attempted to
    take is left charged. Only the unit and the reason: no user id, email,
    lease id or content (emptied, stripped scopes, :mod:`app.ai.alerts`).
    Never raises, never changes the give-back's outcome."""
    unit_tag = _UNIT_TAGS.get(unit.flag, "unknown")
    try:
        alerts.capture_service_warning(
            "ai-quota: a reserved unit could not be given back",
            fingerprint=["ai-quota-unit-kept", reason],
            tags={"unit": unit_tag, "reason": reason},
            extras={"unit": unit_tag, "reason": reason},
        )
    except Exception:  # a warning must never fail a request
        logger.exception("ai_quota: could not send the unit-kept warning")
    logger.warning("ai_quota: unit kept (unit=%s, reason=%s)", unit_tag, reason)


async def refund(db: AsyncSession, reservation: Reservation) -> None:
    """Give back the daily unit (provider unavailable or never reached), at
    most once (:func:`_return_unit`). Never raises; does not release the
    lease (call :func:`release`)."""
    await _return_unit(db, reservation, _daily_unit(reservation), taken=True)


# ---------------------------------------------------------------------------
# Reserve
# ---------------------------------------------------------------------------


async def require_enhanced_entitlement(
    db: AsyncSession, user: User, enhanced: bool
) -> Tier:
    """The user's tier (``entitlements.tier_for``, server-side only); raises
    :class:`EnhancedRequiresPremium` for an enhanced scan by any tier but
    Premium (#533). Used by :func:`reserve` and by ``/scan`` before a scan
    record is served (#585), so feature gating applies to repeats too.
    Takes nothing."""
    tier = await entitlements.tier_for(db, user)
    if enhanced and tier is not Tier.PREMIUM:
        raise EnhancedRequiresPremium("enhanced_requires_premium")
    return tier


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
    """Check the allowances and take the cost estimate, the lease and a
    daily unit. An enhanced scan (#533) takes the same daily unit; there is
    no monthly or enhanced monthly unit (#582).

    ``est_cost_micros`` is the gateway feature's
    ``FeatureSpec.est_cost_ceiling_micros`` (server-side registry value).

    Raises a :class:`QuotaRefusal` (after giving back whatever was taken) when
    the call must not go ahead. Any other exception also gives back what was
    taken before it propagates. On success the caller owns the reservation and
    must :func:`release` it after the provider call.
    """
    # Before anything is taken (#533).
    tier = await require_enhanced_entitlement(db, user, enhanced)
    allowance = allowance_for(ai, tier)
    today = utc_today(now)

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
        tier=tier,
        allowance=allowance,
        est_cost_micros=est_cost_micros,
        ceiling_micros=ceiling,
        enhanced=enhanced,
        zero_result_threshold=ai.zero_result_threshold,
        zero_result_window_hours=ai.zero_result_window_hours,
        zero_result_action=ai.zero_result_action,
    )

    daily = _daily_unit(reservation)
    # Units whose take was attempted, and whether it was acknowledged. After
    # a lost acknowledgement the local state says "attempted, not taken"; the
    # flag on the lease row is the truth (#590).
    attempted: list[_Unit] = []
    taken: set[str] = set()

    async def take(unit: _Unit, limit: int) -> int | None:
        attempted.append(unit)
        used = await take_unit(
            db,
            user.id,
            lease_id,
            unit.flag,
            unit.feature,
            unit.window_kind,
            unit.window_start,
            limit,
        )
        if used is None:
            # Refused, and the refusal was acknowledged: nothing was taken.
            attempted.remove(unit)
        else:
            taken.add(unit.flag)
        return used

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
        if await take(daily, allowance.daily) is None:
            raise DailyLimitReached(retry_after=seconds_until_utc_midnight(now))
        # The upserts above are unbounded, and the lease aged while they ran.
        # Restart its clock so the whole timeout_s + 30 s covers the provider
        # call, and stop if a stall let it expire and another request take it.
        if not await renew_lease(db, user.id, lease_id, seconds):
            raise ScanInProgress("scan_in_progress")
    except BaseException:
        # Includes CancelledError and a take whose commit acknowledgement was
        # lost: give back the unit if its take was attempted, gated on the
        # lease row's flag (a refused or never-landed take
        # gives nothing back), then propagate.
        for unit in reversed(attempted):
            await _return_unit(db, reservation, unit, taken=unit.flag in taken)
        # Releases the lease and gives back the cost estimate.
        await release(db, reservation)
        raise
    return reservation


# ---------------------------------------------------------------------------
# Usage (GET /scan/usage)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Usage:
    """Found scans reserved today (UTC) and the tier's found allowance
    (``used_today`` / ``daily_limit``), and the no-match counter and its
    ceiling (#577). There is no monthly count or limit (#582)."""

    used_today: int
    daily_limit: int
    no_match_used_today: int = 0
    no_match_daily_limit: int = 0


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
    used = await units_used(db, user.id, feature, WINDOW_DAY, today)
    no_match = await units_used(
        db, user.id, SCAN_NO_MATCH_QUOTA_FEATURE, WINDOW_DAY, today
    )
    return Usage(
        used_today=used,
        daily_limit=allowance.daily,
        no_match_used_today=no_match,
        no_match_daily_limit=allowance.no_match,
    )


async def enhanced_remaining(db: AsyncSession, reservation: Reservation) -> int | None:
    """Enhanced scans the user can still make today after the scan: the
    shared daily allowance left, since an enhanced scan takes the shared
    daily unit and nothing else (#582; #583 adds a daily Sonnet counter).
    ``None`` for every tier but Premium (enhanced is Premium-only, #533).
    Ends its read transaction."""
    if reservation.tier is not Tier.PREMIUM:
        return None
    try:
        used = await units_used(
            db,
            reservation.user_id,
            reservation.feature,
            WINDOW_DAY,
            reservation.day,
        )
    finally:
        await db.commit()
    return max(0, reservation.allowance.daily - used)


async def enhanced_remaining_for(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    tier: Tier,
    ai: AISettings,
    now: datetime | None = None,
) -> int | None:
    """As :func:`enhanced_remaining`, with no reservation (a repeat served
    from a scan record, #585, which takes nothing): ``None`` for every tier
    but Premium, else today's shared daily allowance left. Ends its read
    transaction."""
    if tier is not Tier.PREMIUM:
        return None
    try:
        used = await units_used(
            db, user_id, SCAN_QUOTA_FEATURE, WINDOW_DAY, utc_today(now)
        )
    finally:
        await db.commit()
    return max(0, allowance_for(ai, tier).daily - used)


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
    are purged. Day rows are kept for 90 days; month rows (no longer written
    since #582) for the current month and the 13 before it, so they age out
    and a rollback to the previous release finds its counters intact."""
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

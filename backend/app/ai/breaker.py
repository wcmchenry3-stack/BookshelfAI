"""The service-wide daily AI cost circuit breaker and spend alerts (#531).

One ``ai_spend_daily`` row per UTC day holds the spend of every tier and
feature in micro-dollars (1,000,000 = $1):

- ``reserved_micros``: the estimates (``FeatureSpec.est_cost_ceiling_micros``)
  of calls that are reserved but not yet settled;
- ``settled_micros``: what finished calls cost;
- ``alerted_mask``: one bit per alert threshold already sent today.

Reserve
-------
:func:`reserve` runs from ``quota.reserve`` after the global count cap and
before the per-user lease, on the request's session, and commits straight
away (as the quota primitives do). It is one conditional upsert:

- Free (``enforce=True``): ``INSERT ... ON CONFLICT (day) DO UPDATE SET
  reserved = reserved + est WHERE reserved + settled + est <= ceiling``.
  Postgres serialises upserts on the same key, so Free reservations cannot
  take the day's reserved plus settled spend past the ceiling. The total can
  still pass it only by what in-flight calls actually cost over their
  estimate (settled at the real cost) and by Premium spend, which is not
  gated. A
  refusal changes nothing and becomes ``503 ai_unavailable`` before the
  provider is called. The ceiling is the ``ai_settings`` key
  ``breaker.free_daily_ceiling_micros``; 0 pauses every Free scan.
- Premium (``enforce=False``, decision #544): always adds its estimate, so
  the totals and alerts show real spend; Premium is bound only by its own
  quotas and the global count cap.

Settle and release
------------------
:func:`settle` moves the estimate out of ``reserved`` (never below zero) and
adds the call's actual cost to ``settled``. It does not commit:
``quota.release`` executes it with the usage-ledger row, just before the lease
delete commits, so the ledger row, the spend and the lease release land in one
transaction. :func:`release` gives the whole estimate back (a refusal after
the reservation) and commits.

What a call settles (``app/api/scan.py``):

- the provider answered (``ok``/``refused``/``truncated``): its priced cost;
- the provider timed out (``timeout``; the user's units are refunded): the
  estimate, because the request reached the provider and is likely billed;
- the provider was unavailable or never reached (``refunded``, or the
  gateway kill switch; the user's units are refunded too): nothing, the
  estimate is given back;
- any other error after the reservation, or a cancelled request (``error``;
  the units are kept because the provider may have been paid): the estimate
  is kept as settled spend. The ledger records 0 for these (and ``timeout``)
  rows because the real cost is unknown; the breaker counts the estimate so
  it errs on the safe side.

Idempotency: the settle is executed only when the usage-ledger row was
actually inserted in that transaction (``ON CONFLICT (call_id) DO NOTHING
RETURNING``). A retry after a commit whose acknowledgement was lost finds the
row already there, which proves the earlier attempt (spend and lease delete)
committed, so the spend is not added twice.

Alerts
------
After a settle, every threshold in :data:`ALERT_THRESHOLDS_PCT` that the
settled total has reached (``settled * 100 >= pct * ceiling``) and whose bit
is not yet set is claimed with ``UPDATE ... SET alerted_mask = alerted_mask |
bit WHERE alerted_mask & bit = 0 RETURNING``. Only one request can win each
bit per day; the winner sends ONE Sentry event (level ``warning``) with
fingerprint ``["ai-breaker", "<day>", "<pct>"]``, tags ``feature``, ``model``
and ``status`` of the call that crossed it, and the day's totals and ceiling
as extras. Never a user id or any content: the event is captured in a forked,
emptied Sentry scope, so the request's user and tags are not attached (and the
alert's are not left on the request). The event is sent only after the
bit is committed, so it is sent at most once. The thresholds are fixed in code;
the ceiling they are a percentage of is the ``ai_settings`` dial. With a
ceiling of 0 no alerts are sent (Free is paused on purpose).

Retention: :func:`purge_old_spend` deletes rows older than 90 days, from the
daily cleanup task.

This module never calls a provider and must not import ``app.ai.providers``.
"""

import logging
from dataclasses import dataclass
from datetime import date, timedelta

import sentry_sdk
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_spend import AISpendDaily

logger = logging.getLogger(__name__)

# Percent of the Free daily ceiling; bit i of alerted_mask is threshold i.
ALERT_THRESHOLDS_PCT = (50, 75, 90)

RETENTION_DAYS = 90
PURGE_BATCH_SIZE = 5_000
PURGE_MAX_BATCHES = 20


def _micros(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name}={value!r} must be a non-negative int")
    return value


# ---------------------------------------------------------------------------
# Threshold math (pure)
# ---------------------------------------------------------------------------


def threshold_bit(pct: int) -> int:
    return 1 << ALERT_THRESHOLDS_PCT.index(pct)


def crossed_thresholds(settled_micros: int, ceiling_micros: int) -> tuple[int, ...]:
    """The thresholds (percent) that ``settled_micros`` has reached. Integer
    arithmetic only; none when the ceiling is 0."""
    if ceiling_micros <= 0:
        return ()
    return tuple(
        pct
        for pct in ALERT_THRESHOLDS_PCT
        if settled_micros * 100 >= pct * ceiling_micros
    )


def pending_alerts(
    settled_micros: int, ceiling_micros: int, alerted_mask: int
) -> tuple[int, ...]:
    """Crossed thresholds whose alert has not been sent today."""
    return tuple(
        pct
        for pct in crossed_thresholds(settled_micros, ceiling_micros)
        if not alerted_mask & threshold_bit(pct)
    )


# ---------------------------------------------------------------------------
# Reserve / settle / release
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Settlement:
    """What a finished call settles: its actual cost, and the tags its alert
    (if it crosses a threshold) carries. No user id, no content."""

    cost_micros: int
    feature: str
    model: str
    status: str


async def reserve(
    db: AsyncSession,
    day: date,
    est_micros: int,
    ceiling_micros: int,
    *,
    enforce: bool,
) -> bool:
    """Add ``est_micros`` to the day's reserved spend; commit. With
    ``enforce`` (Free) only while ``reserved + settled + est <= ceiling``;
    returns False (nothing changed) when that would be passed."""
    _micros("est_micros", est_micros)
    if enforce and est_micros > ceiling_micros:
        # The insert path (first call of the day) would skip the WHERE.
        return False
    stmt = insert(AISpendDaily).values(
        day=day, reserved_micros=est_micros, settled_micros=0, alerted_mask=0
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[AISpendDaily.day],
        set_={"reserved_micros": AISpendDaily.reserved_micros + est_micros},
        where=(
            (
                AISpendDaily.reserved_micros + AISpendDaily.settled_micros + est_micros
                <= ceiling_micros
            )
            if enforce
            else None
        ),
    ).returning(AISpendDaily.day)
    result = await db.execute(stmt)
    taken = result.scalar_one_or_none() is not None
    await db.commit()
    return taken


async def settle(
    db: AsyncSession, day: date, est_micros: int, cost_micros: int
) -> tuple[int, int, int] | None:
    """Move ``est_micros`` out of reserved (never below zero) and add
    ``cost_micros`` to settled. Does NOT commit (the caller's commit does).
    Returns ``(settled, reserved, alerted_mask)`` after the update, or None
    when the day has no row."""
    _micros("est_micros", est_micros)
    _micros("cost_micros", cost_micros)
    result = await db.execute(
        update(AISpendDaily)
        .where(AISpendDaily.day == day)
        .values(
            reserved_micros=func.greatest(AISpendDaily.reserved_micros - est_micros, 0),
            settled_micros=AISpendDaily.settled_micros + cost_micros,
        )
        .returning(
            AISpendDaily.settled_micros,
            AISpendDaily.reserved_micros,
            AISpendDaily.alerted_mask,
        )
    )
    row = result.one_or_none()
    return None if row is None else (int(row[0]), int(row[1]), int(row[2]))


async def release(db: AsyncSession, day: date, est_micros: int) -> None:
    """Give the whole estimate back (never below zero); commit."""
    _micros("est_micros", est_micros)
    await db.execute(
        update(AISpendDaily)
        .where(AISpendDaily.day == day)
        .values(
            reserved_micros=func.greatest(AISpendDaily.reserved_micros - est_micros, 0)
        )
    )
    await db.commit()


async def totals(db: AsyncSession, day: date) -> tuple[int, int, int]:
    """``(reserved, settled, alerted_mask)`` for ``day`` (zeros if no row)."""
    row = (
        await db.execute(
            select(
                AISpendDaily.reserved_micros,
                AISpendDaily.settled_micros,
                AISpendDaily.alerted_mask,
            ).where(AISpendDaily.day == day)
        )
    ).one_or_none()
    return (0, 0, 0) if row is None else (int(row[0]), int(row[1]), int(row[2]))


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


async def claim_alerts(db: AsyncSession, day: date, pcts: tuple[int, ...]) -> list[int]:
    """Set the bit of each threshold in ``pcts`` not yet set; commit. Returns
    the thresholds THIS call set (each is won by exactly one caller)."""
    won: list[int] = []
    for pct in pcts:
        bit = threshold_bit(pct)
        result = await db.execute(
            update(AISpendDaily)
            .where(AISpendDaily.day == day, AISpendDaily.alerted_mask.op("&")(bit) == 0)
            .values(alerted_mask=AISpendDaily.alerted_mask.op("|")(bit))
            .returning(AISpendDaily.day)
        )
        if result.scalar_one_or_none() is not None:
            won.append(pct)
    await db.commit()
    return won


def send_alert(
    *,
    day: date,
    pct: int,
    settled_micros: int,
    reserved_micros: int,
    ceiling_micros: int,
    settlement: Settlement,
) -> None:
    """One Sentry warning for ``pct`` on ``day``. Never raises."""
    try:
        # The request's scopes carry the user id (set_sentry_user in
        # get_current_user), tags and breadcrumbs. The alert is about the
        # service, not a user: fork both scopes, empty them, and capture there,
        # so nothing of the request leaks in and nothing of the alert leaks
        # into later events of the same request.
        with sentry_sdk.isolation_scope() as scope:
            scope.clear()
            current = sentry_sdk.get_current_scope()
            current.clear()
            for key, value in {
                "feature": settlement.feature,
                "model": settlement.model,
                "status": settlement.status,
                "breaker_threshold_pct": str(pct),
            }.items():
                current.set_tag(key, value)
            for key, value in {
                "day": day.isoformat(),
                "threshold_pct": pct,
                "settled_micros": settled_micros,
                "reserved_micros": reserved_micros,
                "ceiling_micros": ceiling_micros,
            }.items():
                current.set_extra(key, value)
            current.fingerprint = ["ai-breaker", day.isoformat(), str(pct)]
            sentry_sdk.capture_message(
                f"ai-breaker: AI spend reached {pct}% of the Free daily ceiling",
                level="warning",
            )
        logger.warning(
            "ai_breaker: spend reached %d%% of the Free daily ceiling (%s)",
            pct,
            day.isoformat(),
        )
    except Exception:  # an alert must never fail a request
        logger.exception("ai_breaker: could not send the %d%% alert", pct)


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


async def purge_old_spend(db: AsyncSession, today: date) -> int:
    """Delete rows for days more than 90 days before ``today``, in bounded
    batches; return the count."""
    cutoff = today - timedelta(days=RETENTION_DAYS)
    total = 0
    for _ in range(PURGE_MAX_BATCHES):
        batch = (
            select(AISpendDaily.day)
            .where(AISpendDaily.day < cutoff)
            .limit(PURGE_BATCH_SIZE)
        )
        result = await db.execute(
            delete(AISpendDaily).where(AISpendDaily.day.in_(batch))
        )
        await db.commit()
        deleted = result.rowcount or 0
        total += deleted
        if deleted < PURGE_BATCH_SIZE:
            break
    return total

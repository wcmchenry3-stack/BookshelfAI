"""The no-match move (#577): a found unit becomes a no-match unit."""

import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import quota
from app.ai.quota.constants import (
    FLAG_DAILY,
    NO_MATCH_COUNTED,
    NO_MATCH_FREE,
    SCAN_NO_MATCH_QUOTA_FEATURE,
)
from app.ai.quota.types import NoMatchMoved, Reservation
from app.models.ai_quota import WINDOW_DAY, AIInflight, AIQuota

logger = logging.getLogger(__package__)


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
    unit_flag = quota._flag_column(FLAG_DAILY)
    try:
        async with db.begin_nested():
            held = await db.execute(
                select(AIInflight.lease_id)
                .where(
                    quota._held(reservation.user_id, reservation.lease_id),
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
                    quota._unit_upsert(
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
                    quota._tier_tag(reservation.tier),
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
                .where(quota._held(reservation.user_id, reservation.lease_id))
                .values({FLAG_DAILY: False})
            )
            count = int(taken)
            warn = count == ceiling
            if warn:
                logger.warning(
                    "ai_quota: a user's no-match scans reached the daily ceiling "
                    "(tier=%s, ceiling=%d)",
                    quota._tier_tag(reservation.tier),
                    ceiling,
                )
            return NoMatchMoved(NO_MATCH_FREE, count, warn)
    except Exception:
        logger.exception("ai_quota: no-match move failed; the scan stays charged")
        return None

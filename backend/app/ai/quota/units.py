"""Units in ``ai_quota``: take, give back, count (each commits)."""

import uuid
from datetime import date

from sqlalchemy import and_, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import quota
from app.ai.quota.constants import (
    NON_AI_QUOTA_FEATURES,
    UNIT_FLAGS,
    UNIT_GIVEN_BACK,
    UNIT_LEASE_LOST,
    UNIT_NOT_TAKEN,
)
from app.ai.quota.types import ScanInProgress
from app.models.ai_quota import WINDOW_DAY, AIInflight, AIQuota


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
        quota._unit_upsert(user_id, feature, window_kind, window_start, limit)
    )
    used = result.scalar_one_or_none()
    await db.commit()
    return int(used) if used is not None else None


def _flag_column(flag: str):
    if flag not in UNIT_FLAGS:
        raise ValueError(f"unknown unit flag: {flag}")
    return getattr(AIInflight, flag)


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
    quota._flag_column(flag)
    if limit <= 0:
        # Refuses without writing, as reserve_unit.
        return None
    marked = await db.execute(
        update(AIInflight)
        .where(quota._held(user_id, lease_id))
        .values({flag: True})
        .returning(AIInflight.lease_id)
    )
    if marked.scalar_one_or_none() is None:
        await db.commit()  # nothing was written
        raise ScanInProgress("scan_in_progress")
    result = await db.execute(
        quota._unit_upsert(user_id, feature, window_kind, window_start, limit)
    )
    used = result.scalar_one_or_none()
    if used is None:
        # Refused: clear the flag again in the same transaction (each unit is
        # taken at most once per lease, so it was clear before). Not a
        # rollback, which would expire the caller's ORM objects.
        await db.execute(
            update(AIInflight)
            .where(quota._held(user_id, lease_id))
            .values({flag: False})
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
    column = quota._flag_column(flag)
    cleared = await db.execute(
        update(AIInflight)
        .where(quota._held(user_id, lease_id), column.is_(True))
        .values({flag: False})
        .returning(AIInflight.lease_id)
    )
    if cleared.scalar_one_or_none() is None:
        held = await db.execute(
            select(AIInflight.lease_id).where(quota._held(user_id, lease_id))
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
            quota.global_units_filter(day)
        )
    )
    return int(result.scalar_one())

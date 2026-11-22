"""The in-flight lease (table ``ai_inflight``): one paid call per user."""

import uuid
from datetime import timedelta

from sqlalchemy import DateTime, and_, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import quota
from app.ai.quota.constants import UNIT_FLAGS
from app.models.ai_quota import AIInflight


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
        lease_expires_at=quota._clock_timestamp() + timedelta(seconds=seconds),
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
        where=AIInflight.lease_expires_at < quota._clock_timestamp(),
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
        .values(lease_expires_at=quota._clock_timestamp() + timedelta(seconds=seconds))
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


def _held(user_id: uuid.UUID, lease_id: uuid.UUID):
    return and_(AIInflight.user_id == user_id, AIInflight.lease_id == lease_id)


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

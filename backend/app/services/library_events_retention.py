"""Retention of the library audit log (#684): the only code that deletes
``library_events`` rows.

The daily cleanup (``app/services/token_cleanup.py``) calls
:func:`purge_old_library_events`, which deletes events older than
``settings.library_events_retention_days`` (default 180). It works like the
trash purge (``app/services/trash.py``): at most :data:`PURGE_MAX_BATCHES`
batches of :data:`PURGE_BATCH_SIZE` rows a run, each its own commit, each
starting with ``pg_try_advisory_xact_lock(PURGE_LOCK_KEY)``. A held lock
ends the run (another instance is purging), and the ``DELETE`` re-checks
the cutoff, so a repeated run is harmless.

``tests/security/test_library_events_append_only.py`` fails if any other module
in ``app/`` updates or deletes ``library_events``. Logs counts only.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.library_event import LibraryEvent

logger = logging.getLogger(__name__)

PURGE_BATCH_SIZE = 5_000
PURGE_MAX_BATCHES = 20
# Advisory lock key: any constant the app uses for nothing else.
# 0x6c69626576 is "libev" in ASCII.
PURGE_LOCK_KEY = 0x6C69626576


def retention_cutoff(now: datetime | None = None) -> datetime:
    """Events created before this are deleted."""
    return (now or datetime.now(UTC)) - timedelta(
        days=settings.library_events_retention_days
    )


async def _try_lock(db: AsyncSession) -> bool:
    result = await db.execute(select(func.pg_try_advisory_xact_lock(PURGE_LOCK_KEY)))
    return bool(result.scalar_one())


async def purge_old_library_events(
    db: AsyncSession, now: datetime | None = None
) -> int:
    """Delete events older than :func:`retention_cutoff`; return how many.
    See the module docstring for batching and locking."""
    cutoff = retention_cutoff(now)
    total = 0
    for _ in range(PURGE_MAX_BATCHES):
        if not await _try_lock(db):
            await db.rollback()
            logger.info(
                "library_events: purge skipped, another instance holds the lock"
            )
            break
        ids = list(
            (
                await db.execute(
                    select(LibraryEvent.id)
                    .where(LibraryEvent.created_at < cutoff)
                    .order_by(LibraryEvent.created_at)
                    .limit(PURGE_BATCH_SIZE)
                )
            ).scalars()
        )
        if ids:
            result = await db.execute(
                delete(LibraryEvent)
                .where(LibraryEvent.id.in_(ids), LibraryEvent.created_at < cutoff)
                .execution_options(synchronize_session=False)
            )
            total += result.rowcount or 0
        # Commits the batch and releases the advisory lock.
        await db.commit()
        if len(ids) < PURGE_BATCH_SIZE:
            break
    return total

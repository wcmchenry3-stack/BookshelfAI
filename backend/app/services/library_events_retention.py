"""Retention of the library audit log (#684): the only code that purges
``library_events`` rows.

The daily cleanup (``app/services/token_cleanup.py``) calls
:func:`purge_old_library_events`, which deletes events older than
``settings.library_events_retention_days`` (default 180). It works like the
trash purge (``app/services/trash.py``): at most :data:`PURGE_MAX_BATCHES`
batches of :data:`PURGE_BATCH_SIZE` rows a run, each its own commit, each
starting with ``pg_try_advisory_xact_lock(PURGE_LOCK_KEY)``. A held lock
ends the run (another instance is purging), and the delete re-checks the
cutoff, so a repeated run is harmless.

The app role has no ``DELETE`` on ``library_events`` (#1042, migration 0035):
each batch calls the ``SECURITY DEFINER`` function
``purge_library_events(cutoff, batch)``, which deletes at most ``batch`` rows
older than the cutoff and never anything younger than 30 days, whatever
cutoff it is given. The call runs under the job session's
``statement_timeout``; the function sets its own ``lock_timeout``.

``tests/security/test_library_events_append_only.py`` fails if any module in
``app/`` updates or deletes ``library_events``, or if another module calls the
purge function. Logs counts only.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import Integer, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings

logger = logging.getLogger(__name__)

# At most 10,000: the purge function (migration 0035) refuses a larger batch.
PURGE_BATCH_SIZE = 5_000
PURGE_MAX_BATCHES = 20
# Advisory lock key: any constant the app uses for nothing else.
# 0x6c69626576 is "libev" in ASCII.
PURGE_LOCK_KEY = 0x6C69626576


def retention_cutoff(now: datetime | None = None) -> datetime:
    """Events created before this are deleted (but never one younger than
    30 days: the purge function's floor, migration 0035)."""
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
        deleted = (
            await db.execute(
                select(
                    func.purge_library_events(cutoff, PURGE_BATCH_SIZE, type_=Integer)
                )
            )
        ).scalar_one()
        total += deleted
        # Commits the batch and releases the advisory lock.
        await db.commit()
        if deleted < PURGE_BATCH_SIZE:
            break
    return total

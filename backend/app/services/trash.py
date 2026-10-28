"""Trash purge (#683): hard-delete library entries trashed more than
``settings.trash_retention_days`` (30) days ago.

``DELETE /user-books/{id}`` only sets ``deleted_at`` (on the ``user_books``
row and, for an unverified entry, its ``user_custom_books`` row). The daily
cleanup (``app/services/token_cleanup.py``) calls :func:`purge_trash`, which:

- deletes in batches of :data:`PURGE_BATCH_SIZE` rows, one transaction (and
  one commit) per batch, at most :data:`PURGE_MAX_BATCHES` batches per run;
  anything left waits for the next run;
- takes the transaction-level advisory lock :data:`PURGE_LOCK_KEY`
  (``pg_try_advisory_xact_lock``) at the start of every batch, so two
  instances (Render runs two during a deploy) never purge at once. If the
  lock is held elsewhere the run stops there and returns what it deleted so
  far; it never waits for it;
- locks its batch ``FOR UPDATE SKIP LOCKED``, so a row a restore is working
  on is left for the next run rather than waited for;
- deletes the batch's ``user_custom_books`` rows in the same transaction as
  their library rows. ``user_books.custom_book_id`` references
  ``user_custom_books`` (``ON DELETE CASCADE`` from the custom book to the
  library row), so the library rows go first and nothing is orphaned;
- is idempotent: it deletes only rows that are still trashed and past the
  cutoff, so running it again (or after a partial run) deletes nothing twice.

Logs counts only, never ids or titles.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.user_book import UserBook
from app.models.user_custom_book import UserCustomBook

logger = logging.getLogger(__name__)

PURGE_BATCH_SIZE = 500
PURGE_MAX_BATCHES = 20
# Advisory lock key for the purge: any constant the app uses for nothing
# else. 0x7472617368 is "trash" in ASCII.
PURGE_LOCK_KEY = 0x7472617368


def purge_cutoff(now: datetime | None = None) -> datetime:
    """Entries trashed before this are purged."""
    return (now or datetime.now(UTC)) - timedelta(days=settings.trash_retention_days)


async def _try_lock(db: AsyncSession) -> bool:
    result = await db.execute(select(func.pg_try_advisory_xact_lock(PURGE_LOCK_KEY)))
    return bool(result.scalar_one())


async def purge_trash(db: AsyncSession, now: datetime | None = None) -> int:
    """Delete library entries trashed before :func:`purge_cutoff`, with
    their unverified entries; return how many library rows were deleted.
    See the module docstring for batching, locking and idempotency."""
    cutoff = purge_cutoff(now)
    total = 0
    for _ in range(PURGE_MAX_BATCHES):
        if not await _try_lock(db):
            await db.rollback()
            logger.info("trash: purge skipped, another instance holds the lock")
            break
        rows = (
            await db.execute(
                select(UserBook.id, UserBook.custom_book_id)
                .where(
                    UserBook.deleted_at.is_not(None),
                    UserBook.deleted_at < cutoff,
                )
                .order_by(UserBook.deleted_at)
                .limit(PURGE_BATCH_SIZE)
                .with_for_update(skip_locked=True)
            )
        ).all()
        if rows:
            await db.execute(
                delete(UserBook).where(UserBook.id.in_([r.id for r in rows]))
            )
            custom_ids = [r.custom_book_id for r in rows if r.custom_book_id]
            if custom_ids:
                await db.execute(
                    delete(UserCustomBook).where(UserCustomBook.id.in_(custom_ids))
                )
        # Commits the batch and releases the advisory lock.
        await db.commit()
        total += len(rows)
        if len(rows) < PURGE_BATCH_SIZE:
            break
    return total

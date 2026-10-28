"""The trash (#683): its bounds, and the purge that ends it.

``DELETE /user-books/{id}`` only sets ``deleted_at`` (on the ``user_books``
row and, for an unverified entry, its ``user_custom_books`` row). The trash
is bounded two ways, whichever comes first:

- **Count.** At most ``settings.trash_max_entries_per_user`` (100) entries
  per user. ``DELETE`` calls :func:`evict_over_cap` in its own transaction,
  under the user's row lock, which hard-deletes the user's oldest trashed
  entries beyond the cap. So a delete loop (add, delete, add again) never
  grows the table past live rows + the cap.
- **Age.** The daily cleanup (``app/services/token_cleanup.py``) calls
  :func:`purge_trash`, which deletes entries trashed more than
  ``settings.trash_retention_days`` (30) days ago:

  - in batches of :data:`PURGE_BATCH_SIZE` rows, one transaction (and one
    commit) per batch, at most :data:`PURGE_MAX_BATCHES` batches per run;
    anything left waits for the next run;
  - under the transaction-level advisory lock :data:`PURGE_LOCK_KEY`
    (``pg_try_advisory_xact_lock``), taken at the start of every batch, so
    two instances (Render runs two during a deploy) never purge at once. If
    the lock is held elsewhere the run stops there; it never waits for it;
  - locking its batch ``FOR UPDATE SKIP LOCKED``, so a row a restore is
    working on is left for the next run rather than waited for, and with
    the ``DELETE`` re-checking that the row is still trashed and past the
    cutoff, so a row restored in between is never deleted;
  - idempotently: it deletes only rows that are still trashed and past the
    cutoff.

Both delete a library row's ``user_custom_books`` row in the same
transaction, and only for library rows actually deleted
(:func:`_delete_entries`). ``user_books.custom_book_id`` references
``user_custom_books`` (``ON DELETE CASCADE`` from the custom book to the
library row), so the library rows go first and nothing is orphaned. Lock
order everywhere: ``users`` → ``user_books`` → ``user_custom_books``.

Logs counts only, never ids or titles.
"""

import logging
import uuid
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


async def _delete_entries(db: AsyncSession, ids: list[uuid.UUID], *conditions) -> int:
    """Hard-delete the trashed ``user_books`` rows ``ids`` that still match
    ``conditions`` (re-checked by the ``DELETE`` itself), then the
    ``user_custom_books`` rows of exactly the rows deleted. Returns how many
    library rows were deleted. Does not commit."""
    if not ids:
        return 0
    deleted = (
        await db.execute(
            delete(UserBook)
            .where(UserBook.id.in_(ids), UserBook.deleted_at.is_not(None), *conditions)
            .returning(UserBook.custom_book_id)
            .execution_options(synchronize_session=False)
        )
    ).all()
    custom_ids = [r.custom_book_id for r in deleted if r.custom_book_id is not None]
    if custom_ids:
        await db.execute(
            delete(UserCustomBook)
            .where(UserCustomBook.id.in_(custom_ids))
            .execution_options(synchronize_session=False)
        )
    return len(deleted)


async def evict_over_cap(db: AsyncSession, user_id: uuid.UUID) -> int:
    """Hard-delete the user's oldest trashed entries beyond
    ``settings.trash_max_entries_per_user`` (newest deletion kept; ties by
    id). Call with the user locked (``unverified_books.lock_user``), in the
    transaction that trashed the entry. Returns how many were deleted. Does
    not commit."""
    over = (
        await db.execute(
            select(UserBook.id)
            .where(UserBook.user_id == user_id, UserBook.deleted_at.is_not(None))
            .order_by(UserBook.deleted_at.desc(), UserBook.id.desc())
            .offset(settings.trash_max_entries_per_user)
            .with_for_update()
        )
    ).scalars()
    evicted = await _delete_entries(db, list(over), UserBook.user_id == user_id)
    if evicted:
        logger.info("trash: evicted %d entr(ies) over the per-user cap", evicted)
    return evicted


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
        ids = list(
            (
                await db.execute(
                    select(UserBook.id)
                    .where(
                        UserBook.deleted_at.is_not(None),
                        UserBook.deleted_at < cutoff,
                    )
                    .order_by(UserBook.deleted_at)
                    .limit(PURGE_BATCH_SIZE)
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        total += await _delete_entries(db, ids, UserBook.deleted_at < cutoff)
        # Commits the batch and releases the advisory lock.
        await db.commit()
        if len(ids) < PURGE_BATCH_SIZE:
            break
    return total

"""The trash (#683): its bounds, and the purge that ends it.

``DELETE /user-books/{id}`` only sets ``deleted_at`` (on the ``user_books``
row and, for an unverified entry, its ``user_custom_books`` row). The trash
is bounded three ways:

- **One empty copy per book.** ``DELETE`` calls :func:`evict_same_book`
  in the transaction that trashes the entry, under the user's row lock,
  which hard-deletes the user's other (older) trashed entries of the same
  book that hold no data of the user's (``UserBook.has_no_user_data``: no
  notes, rating, edition, overrides, status beyond wishlisted or status
  dates). The same book is, for a catalog entry, any row of the same work
  (``catalog.of_work``, merged rows included, #682); for an unverified
  entry the same title and author or ISBN (``unverified_books.same_book``,
  the duplicate check's key). So an add / delete loop on one book never
  grows the trash, and a copy with the user's data is never replaced by a
  later one: it stays restorable until the purge or the backstop cap
  (owner decisions, 2026-10-28). The just-trashed entry is never evicted.
- **Count (backstop).** At most ``settings.trash_max_entries_per_user``
  (1,000) entries per user. ``DELETE`` then calls :func:`evict_over_cap`,
  which hard-deletes the user's oldest trashed entries beyond the cap,
  whatever they hold: the only way a copy with the user's data leaves the
  trash before the purge. A mass delete of up to 1,000 distinct books stays
  fully recoverable; beyond that the oldest are lost.
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

All three delete a library row's ``user_custom_books`` row in the same
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
from app.services.catalog import of_work
from app.services.unverified_books import same_book

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


async def evict_same_book(db: AsyncSession, user_id: uuid.UUID, entry: UserBook) -> int:
    """Hard-delete ``user_id``'s other trashed entries of ``entry``'s book
    that hold no data of the user's (:meth:`UserBook.has_no_user_data`):
    for a catalog entry the trashed entries on a row of the same work
    (``entry`` needs ``book`` loaded), for an unverified entry the trashed
    ones with the same title and author or ISBN (``entry`` needs
    ``custom_book`` loaded). ``entry`` itself (just trashed) is never
    evicted, whatever it holds, and nor is an older copy with notes, a
    rating, an edition, overrides or a status past wishlisted: those stay
    until the purge or the backstop cap. Call with the user locked
    (``unverified_books.lock_user``), in the transaction that trashed
    ``entry``.

    The catalog select skips rows the purge holds (``SKIP LOCKED``): it is
    deleting them anyway. The unverified select takes no row locks (it reads
    every trashed unverified entry of the user and matches the book in
    Python, so locking would lock them all); the ``DELETE`` re-checks that
    each row is still trashed, still the user's and still empty, and the
    user lock serialises this with restore. Returns how many were deleted.
    Does not commit."""
    candidates = (
        UserBook.user_id == user_id,
        UserBook.deleted_at.is_not(None),
        UserBook.id != entry.id,
        UserBook.has_no_user_data(),
    )
    custom = entry.custom_book
    if custom is not None:
        rows = (
            await db.execute(
                select(
                    UserBook.id,
                    UserCustomBook.title,
                    UserCustomBook.author,
                    UserCustomBook.isbn_13,
                )
                .join(UserCustomBook, UserBook.custom_book_id == UserCustomBook.id)
                .where(*candidates)
            )
        ).all()
        ids = [
            r.id
            for r in rows
            if same_book(r, custom.title, custom.author, custom.isbn_13)
        ]
    elif entry.book is not None:
        ids = list(
            (
                await db.execute(
                    select(UserBook.id)
                    .where(*candidates, of_work(UserBook.book_id, entry.book))
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
    else:
        return 0
    evicted = await _delete_entries(
        db,
        ids,
        UserBook.user_id == user_id,
        UserBook.id != entry.id,
        UserBook.has_no_user_data(),
    )
    if evicted:
        logger.info("trash: evicted %d older empty trashed cop(ies) of a book", evicted)
    return evicted


async def evict_over_cap(db: AsyncSession, user_id: uuid.UUID) -> int:
    """Hard-delete the user's oldest trashed entries beyond
    ``settings.trash_max_entries_per_user`` (newest deletion kept; ties by
    id, highest kept, the trash list's order). A backstop behind
    :func:`evict_same_book`. Call with the user locked
    (``unverified_books.lock_user``), in the transaction that trashed the
    entry. Rows the purge holds are skipped (``SKIP LOCKED``): it is
    deleting them anyway, and the ``DELETE`` re-checks its predicate.
    Returns how many were deleted. Does not commit."""
    over = (
        await db.execute(
            select(UserBook.id)
            .where(UserBook.user_id == user_id, UserBook.deleted_at.is_not(None))
            .order_by(UserBook.deleted_at.desc(), UserBook.id.desc())
            .offset(settings.trash_max_entries_per_user)
            .with_for_update(skip_locked=True)
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

"""The shared catalog's provenance and merge rules (#682).

Provenance. Every new ``books`` row records which provider supplied it
(``books.source``, :func:`source_of`) and when the server fetched that data
(``books.fetched_at``). ``WishlistService`` is the only code that creates
catalog rows (#602), and only from provider data the server signed.

Merges. When duplicate catalog rows are merged, the duplicate is kept with
``books.merged_into_id`` pointing at the surviving row instead of being
deleted. There is no runtime merge code: the only merge so far is migration
0015 (#613), which deleted its duplicates before this column existed, so no
row has ``merged_into_id`` set yet. A future merge must:

- point ``merged_into_id`` at a row that is not itself merged, and re-point
  rows already merged into the duplicate (so one hop always reaches the
  survivor);
- move the duplicate's ``user_books`` and ``editions`` to the survivor, as
  0015 did (reads would follow ``merged_into_id`` anyway; this keeps one
  library entry per work).

A merged row and its survivor are one work, and library entries on a
merged row can exist (nothing relies on a merge having moved them). Reads
follow ``merged_into_id`` to the survivor (:func:`effective_book` for an
already loaded row, :func:`resolve_survivor` otherwise); the library check
(``deduplication.held_entries``) matches an entry on any ID of its work;
``POST /purchased`` promotes an entry on any row of the work. A merged row
is never the target of a new library entry: the write paths redirect to the
survivor (:func:`resolve_survivor`), so a client holding a stale ``book_id``
or provider ID still adds the same work, once.
"""

import logging
import uuid

from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.book import Book

logger = logging.getLogger(__name__)

GOOGLE_BOOKS = "google_books"
OPEN_LIBRARY = "open_library"
SCAN = "scan"

# Hops followed at most when resolving a merged row; merges keep chains at
# one hop, so more means a broken merge.
MAX_MERGE_HOPS = 5


class MergeChainError(RuntimeError):
    """``merged_into_id`` forms a cycle or a chain longer than
    :data:`MAX_MERGE_HOPS`: a broken merge, never caused by a request."""


def source_of(open_library_work_id: str | None, google_books_id: str | None) -> str:
    """``books.source`` for a row with these provider IDs: Google Books when
    a volume matched (it supplies the description, cover and edition
    details), else Open Library, else ``scan`` (no provider matched; only
    rows from before #602). The same rule as migration 0022's backfill."""
    if google_books_id:
        return GOOGLE_BOOKS
    if open_library_work_id:
        return OPEN_LIBRARY
    return SCAN


async def resolve_survivor(db: AsyncSession, book: Book) -> Book:
    """``book``, or the row it was merged into (followed to the end). The
    same object when ``book`` is not merged. :class:`MergeChainError` on a
    cycle or a chain longer than :data:`MAX_MERGE_HOPS`."""
    seen: set[uuid.UUID] = {book.id}
    hops = 0
    while book.merged_into_id is not None:
        hops += 1
        target = book.merged_into_id
        if hops > MAX_MERGE_HOPS or target in seen:
            raise MergeChainError(f"books.merged_into_id chain broken at {book.id}")
        seen.add(target)
        result = await db.execute(select(Book).where(Book.id == target))
        book = result.scalar_one()
    return book


def effective_book(book: Book | None) -> Book | None:
    """The row a library read shows for ``book``: the survivor it was merged
    into when that is loaded (``selectinload(Book.merged_into)``), else
    ``book``. Never lazy-loads. Follows one hop, as merges keep chains at
    one hop: a survivor that is itself merged is a broken merge and is
    logged (the read still shows the first hop)."""
    if book is None or book.merged_into_id is None:
        return book
    survivor = book.merged_into if "merged_into" not in inspect(book).unloaded else None
    if survivor is None:
        logger.warning("catalog: merged book %s read without its survivor", book.id)
        return book
    if survivor.merged_into_id is not None:
        logger.warning(
            "catalog: books.merged_into_id chain longer than one hop at %s", book.id
        )
    return survivor

"""DeduplicationService — flags books already in the user's collection.

Edition-level (#587). The edition a result stands for is ``editions[0]``:
the one ``POST /wishlist`` links (``WishlistService`` reads only the first
edition), and the only one enrichment produces. Its ISBN-13 (an ISBN-10
normalised to ISBN-13) is compared with the editions in the requesting
user's library:

- the same ISBN as an edition the user has: ``owned_same_edition``;
- the work is in the library (Open Library work ID, else Google Books ID)
  but its edition has a different ISBN: ``owned_other_edition``;
- the work is in the library but either side has no ISBN:
  ``owned_edition_unknown``;
- otherwise ``not_owned``.

Every library entry of the work counts (Open Library work ID *or* Google
Books ID): any entry holding the same edition makes it the same edition;
otherwise any entry holding an edition with an ISBN makes it another edition.
A merged work (#682) is one work: an entry on the survivor or on a row merged
into it matches a result carrying any of those rows' IDs.

Only the requesting user's ``user_books`` count. One query per response,
whatever the number of results: the user's entries (``user_books.user_id``
index) of any of the results' works, volumes or ISBNs, with each entry's
edition ISBNs joined in. ``WishlistService`` uses the same query and rules
for its 409 (#587).
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import array_agg
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.book import Book
from app.models.edition import Edition
from app.models.user_book import UserBook
from app.schemas.book import NOT_OWNED, EnrichedBook, Ownership
from app.services.enrichment import isbn_13_to_10, normalise_isbn_13

logger = logging.getLogger(__name__)

OWNED_SAME_EDITION: Ownership = "owned_same_edition"
OWNED_OTHER_EDITION: Ownership = "owned_other_edition"
OWNED_EDITION_UNKNOWN: Ownership = "owned_edition_unknown"


class _EditionLike(Protocol):
    isbn_13: str | None
    isbn_10: str | None


class _BookLike(Protocol):
    """An ``EnrichedBook`` or a ``WishlistRequest``."""

    editions: list


def edition_key(edition: _EditionLike | None) -> str | None:
    """An edition's comparable ISBN-13, or None when it has no ISBN."""
    if edition is None:
        return None
    return normalise_isbn_13(
        getattr(edition, "isbn_13", None), getattr(edition, "isbn_10", None)
    )


def candidate_key(book: _BookLike) -> str | None:
    """The comparable ISBN-13 of the edition a result stands for
    (``editions[0]``, what POST /wishlist would add)."""
    editions = getattr(book, "editions", None) or []
    return edition_key(editions[0]) if editions else None


# Library entries looked at per query at most; entries with an edition come
# first, so a cut never hides an edition behind an entry without one.
MAX_HELD = 200


@dataclass(frozen=True)
class Held:
    """One of the user's library entries, with its edition's ISBN key.

    ``book_id`` is the survivor's id when the entry's book was merged
    (#682). ``book_ids``, ``work_ids`` and ``volume_ids`` hold every ID of
    the entry's work: the survivor's and those of every row merged into it
    (which includes the entry's own book), so a candidate carrying any of
    them is this entry."""

    book_id: object
    edition_id: object
    open_library_work_id: str | None  # the entry's own book's
    google_books_id: str | None  # the entry's own book's
    isbn: str | None  # normalised ISBN-13 of the entry's edition
    book_ids: frozenset = frozenset()
    work_ids: frozenset[str] = frozenset()
    volume_ids: frozenset[str] = frozenset()


def isbn_lookup_values(isbn_13s: set[str]) -> tuple[set[str], set[str]]:
    """The stored ISBN-13 and ISBN-10 values that normalise to ``isbn_13s``
    (editions may hold only an ISBN-10)."""
    isbn_10s = {i10 for i10 in (isbn_13_to_10(i) for i in isbn_13s) if i10}
    return set(isbn_13s), isbn_10s


async def held_entries(
    db: AsyncSession,
    user_id,
    *,
    book_ids: Iterable = (),
    work_ids: Iterable[str] = (),
    volume_ids: Iterable[str] = (),
    isbns: Iterable[str] = (),
) -> list[Held]:
    """The user's library entries for any of these books, Open Library
    works, Google Books volumes or edition ISBNs (normalised ISBN-13), in
    one join-shaped query driven by the user's entries. No query when there
    is nothing to look for.

    An entry counts for its whole work (#682): it is selected when its
    book, the row its book was merged into, or any row merged into that
    survivor carries a requested ID, and its ``book_id`` is the survivor's.
    Entries on merged rows can exist (a merge moves them, but reads never
    rely on it)."""
    book_ids, work_ids = set(book_ids), set(work_ids)
    volume_ids, isbns = set(volume_ids), set(isbns)
    # The entry's work (#682): its survivor (the entry's book, or the row it
    # was merged into) and every row merged into that survivor. Merges keep
    # chains at one hop, so this is the whole work. Read with correlated
    # subqueries, so ``books`` stays an inner join on the entry's own book.
    survivor_id = func.coalesce(Book.merged_into_id, Book.id)
    family = aliased(Book)
    of_work = or_(family.id == survivor_id, family.merged_into_id == survivor_id)

    def family_ids(column):
        return select(array_agg(column)).where(of_work).scalar_subquery()

    # The entry's own book matches (the usual, unmerged case), or any row
    # of its work does.
    conditions = []
    family_match = []
    if book_ids:
        conditions.append(UserBook.book_id.in_(book_ids))
        family_match.append(family.id.in_(book_ids))
    if work_ids:
        conditions.append(Book.open_library_work_id.in_(work_ids))
        family_match.append(family.open_library_work_id.in_(work_ids))
    if volume_ids:
        conditions.append(Book.google_books_id.in_(volume_ids))
        family_match.append(family.google_books_id.in_(volume_ids))
    if family_match:
        conditions.append(select(family.id).where(of_work, or_(*family_match)).exists())
    if isbns:
        isbn_13s, isbn_10s = isbn_lookup_values(isbns)
        conditions.append(Edition.isbn_13.in_(isbn_13s))
        if isbn_10s:
            conditions.append(Edition.isbn_10.in_(isbn_10s))
    if not conditions:
        return []
    result = await db.execute(
        select(
            UserBook.book_id,
            UserBook.edition_id,
            Book.open_library_work_id,
            Book.google_books_id,
            Edition.isbn_13,
            Edition.isbn_10,
            survivor_id.label("survivor_id"),
            family_ids(family.id).label("family_book_ids"),
            family_ids(family.open_library_work_id).label("family_work_ids"),
            family_ids(family.google_books_id).label("family_volume_ids"),
        )
        .join(Book, Book.id == UserBook.book_id)
        .outerjoin(Edition, Edition.id == UserBook.edition_id)
        .where(UserBook.user_id == user_id, or_(*conditions))
        # Entries with an edition first, then a stable order.
        .order_by(UserBook.edition_id.is_(None), UserBook.created_at, UserBook.id)
        .limit(MAX_HELD)
    )
    return [_held(row) for row in result.all()]


def _ids(values) -> frozenset:
    """An aggregated ID list as a set, without NULLs (rows with no such ID)."""
    return frozenset(v for v in values or () if v is not None)


def _held(row) -> Held:
    """A library query row as :class:`Held`: the survivor's ``book_id``
    (#682), the entry's own provider IDs and every ID of its work."""
    return Held(
        book_id=row.survivor_id,
        edition_id=row.edition_id,
        open_library_work_id=row.open_library_work_id,
        google_books_id=row.google_books_id,
        isbn=normalise_isbn_13(row.isbn_13, row.isbn_10),
        book_ids=_ids(row.family_book_ids),
        work_ids=_ids(row.family_work_ids),
        volume_ids=_ids(row.family_volume_ids),
    )


def matching(
    held: list[Held],
    *,
    work_id: str | None = None,
    volume_id: str | None = None,
    isbn: str | None = None,
    book_id=None,
) -> list[Held]:
    """The entries that are this book: the same catalog row, Open Library
    work, Google Books volume or edition ISBN. A merged work (#682) matches
    on any of its rows' IDs (the survivor's or a merged row's)."""
    return [
        h
        for h in held
        if (book_id is not None and (h.book_id == book_id or book_id in h.book_ids))
        or (work_id and (h.open_library_work_id == work_id or work_id in h.work_ids))
        or (volume_id and (h.google_books_id == volume_id or volume_id in h.volume_ids))
        or (isbn and h.isbn == isbn)
    ]


def ownership_of(
    isbn: str | None, matches: list[Held], *, edition_id=None
) -> Ownership:
    """Ownership of a book (edition key ``isbn``, catalog ``edition_id``)
    from all the library entries that are this book: the same edition if
    any entry holds it; else another edition if any entry's edition has an
    ISBN to compare; else unknown."""
    if not matches:
        return NOT_OWNED
    if any(
        (isbn and h.isbn == isbn)
        or (edition_id is not None and h.edition_id == edition_id)
        for h in matches
    ):
        return OWNED_SAME_EDITION
    if isbn and any(h.isbn for h in matches):
        return OWNED_OTHER_EDITION
    return OWNED_EDITION_UNKNOWN


class DeduplicationService:
    async def check(
        self, db: AsyncSession, user_id: str, candidates: list[EnrichedBook]
    ) -> list[EnrichedBook]:
        """Set ``ownership`` (and ``already_in_library``) on every candidate
        from the user's own library, in one query."""
        keys = [candidate_key(c) for c in candidates]
        held = await held_entries(
            db,
            user_id,
            work_ids={
                c.open_library_work_id for c in candidates if c.open_library_work_id
            },
            volume_ids={c.google_books_id for c in candidates if c.google_books_id},
            isbns={k for k in keys if k},
        )
        for candidate, isbn in zip(candidates, keys, strict=True):
            matches = matching(
                held,
                work_id=candidate.open_library_work_id,
                volume_id=candidate.google_books_id,
                isbn=isbn,
            )
            candidate.ownership = ownership_of(isbn, matches)
            candidate.already_in_library = candidate.ownership != NOT_OWNED
        return candidates

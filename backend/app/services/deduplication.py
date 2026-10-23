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

Only the requesting user's ``user_books`` count. Queries: one for all the
results' ISBNs together (skipped when none has one), then at most two per
result (work ID, then Google Books ID), as before #587.
"""

import logging
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import contains_eager, selectinload

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


def classify(
    candidate_isbn: str | None, owned_edition: _EditionLike | None
) -> Ownership:
    """Ownership of a result whose work matched a library entry holding
    ``owned_edition`` (None when the entry has no edition)."""
    owned_isbn = edition_key(owned_edition)
    if candidate_isbn and owned_isbn:
        if candidate_isbn == owned_isbn:
            return OWNED_SAME_EDITION
        return OWNED_OTHER_EDITION
    return OWNED_EDITION_UNKNOWN


def isbn_lookup_values(isbn_13s: set[str]) -> tuple[set[str], set[str]]:
    """The stored ISBN-13 and ISBN-10 values that normalise to ``isbn_13s``
    (editions may hold only an ISBN-10)."""
    isbn_10s = {i10 for i10 in (isbn_13_to_10(i) for i in isbn_13s) if i10}
    return set(isbn_13s), isbn_10s


class DeduplicationService:
    async def check(
        self, db: AsyncSession, user_id: str, candidates: list[EnrichedBook]
    ) -> list[EnrichedBook]:
        """Set ``ownership`` (and ``already_in_library``) on every candidate
        from the user's own library."""
        owned_isbns = await self._owned_isbns(db, user_id, candidates)
        for candidate in candidates:
            ownership = await self._ownership(db, user_id, candidate, owned_isbns)
            candidate.ownership = ownership
            candidate.already_in_library = ownership != NOT_OWNED
        return candidates

    async def _owned_isbns(
        self, db: AsyncSession, user_id: str, candidates: list[EnrichedBook]
    ) -> set[str]:
        """The candidates' ISBNs that are editions in the user's library,
        in one query."""
        wanted = {k for k in (candidate_key(c) for c in candidates) if k}
        if not wanted:
            return set()
        isbn_13s, isbn_10s = isbn_lookup_values(wanted)
        result = await db.execute(
            select(UserBook)
            .join(Edition, Edition.id == UserBook.edition_id)
            .where(
                UserBook.user_id == user_id,
                or_(Edition.isbn_13.in_(isbn_13s), Edition.isbn_10.in_(isbn_10s)),
            )
            .options(contains_eager(UserBook.edition))
        )
        owned = {edition_key(ub.edition) for ub in result.scalars().all()}
        return {k for k in owned if k} & wanted

    async def _ownership(
        self,
        db: AsyncSession,
        user_id: str,
        candidate: EnrichedBook,
        owned_isbns: set[str],
    ) -> Ownership:
        isbn = candidate_key(candidate)
        if isbn and isbn in owned_isbns:
            return OWNED_SAME_EDITION
        entry = await self._work_entry(db, user_id, candidate)
        if entry is None:
            return NOT_OWNED
        return classify(isbn, entry.edition)

    async def _work_entry(
        self, db: AsyncSession, user_id: str, candidate: EnrichedBook
    ) -> UserBook | None:
        """A library entry of the candidate's work, with its edition."""
        if candidate.open_library_work_id:
            result = await db.execute(
                select(UserBook)
                .join(Book, Book.id == UserBook.book_id)
                .where(
                    Book.open_library_work_id == candidate.open_library_work_id,
                    UserBook.user_id == user_id,
                )
                .options(selectinload(UserBook.edition))
                # Existence only: one row is enough.
                .limit(1)
            )
            entry = result.scalar_one_or_none()
            if entry:
                return entry

        if candidate.google_books_id:
            result = await db.execute(
                select(UserBook)
                .join(Book, Book.id == UserBook.book_id)
                .where(
                    Book.google_books_id == candidate.google_books_id,
                    UserBook.user_id == user_id,
                )
                .options(selectinload(UserBook.edition))
                # google_books_id is not unique (#613): with duplicate Book
                # rows the user may hold several, which must not raise
                # MultipleResultsFound.
                .limit(1)
            )
            entry = result.scalar_one_or_none()
            if entry:
                return entry

        return None

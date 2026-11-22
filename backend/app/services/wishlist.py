"""WishlistService — finds or creates Book + Edition, creates/retrieves UserBook.

The shared catalog (``books``, ``editions``) is written only from
provider data the server itself produced (#602):

- A book with no provider ID (no ``open_library_work_id`` and no
  ``google_books_id``) never creates or links a catalog row: nothing
  matched it, so its title and author are only the vision model's reading.
  Refused with :class:`BookNotInCatalogError` (422 ``book_not_in_catalog``).
- A request whose ``catalog_token`` verifies (the server signed exactly
  these fields for this user, and signs only provider-sourced catalog
  fields, see ``app/core/catalog_token.py``) may create a missing Book /
  Edition.
- Any other request may only link the user to a Book that already exists
  (and an existing Edition of that Book), else it is refused with
  :class:`CatalogTokenInvalidError` (422 ``catalog_token_invalid``).

A library holds one live entry per title (``uq_user_books_user_book_live``;
an entry in the trash does not count, so a trashed book can be added again
as a new entry, #683), so a book the user already has is refused with
:class:`AlreadyOwnedError` (409, #587) and no row is created or changed: the same edition (the same ISBN-13, an
ISBN-10 normalised, or the same ``edition_id``) is ``owned_same_edition``,
another edition of the work ``owned_other_edition``, and a match where one
side has no ISBN ``owned_edition_unknown``. The work matches on the resolved
Book, the Open Library work ID or the Google Books ID, with the same query
and rules as ``app/services/deduplication.py``, which marks scan and search
results, so the 409 agrees with what the scan showed.

A user book is never linked to an Edition of a different Book, signed or
not. Existing catalog rows are never modified from a request. Creation is
idempotent per work: rows are matched by provider ID first, and a
concurrent first add of the same Open Library work (unique
``books.open_library_work_id``), Google Books volume (partial unique index
``uq_books_google_books_id``, #613) or ISBN-13 (unique ``editions.isbn_13``)
re-reads and links the row the other request created instead of failing.
Lookups by ``google_books_id`` still take the oldest row (``created_at``,
then ``id``) and never fail on duplicates (#611); since the index (migration
0015, which merged the duplicates that existed) none can be created.
"""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import catalog_token
from app.models.book import Book
from app.models.edition import Edition
from app.models.user_book import UserBook
from app.schemas.book import NOT_OWNED, Ownership
from app.schemas.user_book import WishlistRequest
from app.services import catalog
from app.services.deduplication import (
    candidate_key,
    held_entries,
    matching,
    ownership_of,
)

logger = logging.getLogger(__name__)


class CatalogTokenInvalidError(Exception):
    """No valid catalog token and no existing Book to link to (422
    ``catalog_token_invalid``)."""


class BookNotInCatalogError(Exception):
    """The book has no provider ID, so it can never be in the shared
    catalog (422 ``book_not_in_catalog``)."""


class AlreadyOwnedError(Exception):
    """The user already has this book (409, ``detail`` = :attr:`ownership`,
    #587). Nothing was written."""

    def __init__(self, ownership: Ownership):
        super().__init__(ownership)
        self.ownership = ownership


class WishlistService:
    async def add(
        self, db: AsyncSession, user_id: uuid.UUID, req: WishlistRequest
    ) -> UserBook:
        if not catalog_token.is_signable(req):
            raise BookNotInCatalogError
        reason = catalog_token.check(req.catalog_token, req, user_id)
        trusted = reason == catalog_token.OK
        if not trusted:
            # The reason only, never the token.
            logger.info("wishlist: catalog token not valid (%s)", reason)
        book = await self._find_or_create_book(db, req, create=trusted)
        if book is None:
            raise CatalogTokenInvalidError
        edition = await self._find_or_create_edition(db, book.id, req, create=trusted)
        try:
            user_book = await self._find_or_create_user_book(
                db,
                user_id,
                book.id,
                edition.id if edition else None,
                isbn=candidate_key(req),
                work_id=req.open_library_work_id,
                volume_id=req.google_books_id,
            )
        except AlreadyOwnedError as exc:
            # Nothing is written for a book the user already has, not even
            # a catalog row created above.
            await db.rollback()
            logger.info("wishlist: already in the library (%s)", exc.ownership)
            raise
        await db.commit()
        await db.refresh(user_book, ["book", "edition"])
        # Eagerly load book.editions for serialisation
        await db.refresh(book, ["editions"])
        return user_book

    async def _find_or_create_book(
        self, db: AsyncSession, req: WishlistRequest, *, create: bool = False
    ) -> Book | None:
        """The Book matching the work-level IDs; otherwise a new one from
        ``req`` when ``create`` (a verified catalog token), else None."""
        book = await self._find_book(db, req)
        if book is not None:
            return book

        if not create:
            return None

        # Not found — create (from server-signed data only)
        if not catalog_token.is_signable(req):
            # Defence in depth: never create a row no provider ID identifies.
            return None

        book = Book(
            open_library_work_id=req.open_library_work_id,
            google_books_id=req.google_books_id,
            title=req.title,
            author=req.author,
            description=req.description,
            cover_url=req.cover_url,
            subjects=req.subjects or [],
            # Provenance (#682): signed provider data, fetched by the server
            # for the scan or search this token came from.
            source=catalog.source_of(req.open_library_work_id, req.google_books_id),
            fetched_at=datetime.now(UTC),
        )
        try:
            async with db.begin_nested():
                db.add(book)
                await db.flush()  # get book.id without committing
        except IntegrityError:
            # Another request created this work first (unique
            # open_library_work_id, or uq_books_google_books_id, #613): link
            # its row, found by either ID.
            existing = await self._find_book(db, req)
            if existing is None:
                raise
            return existing
        return book

    async def _find_book(self, db: AsyncSession, req: WishlistRequest) -> Book | None:
        """The Book with ``req``'s Open Library work ID, else its Google
        Books ID, else None. A merged row is redirected to the row it was
        merged into (#682): no new library entry targets a merged row."""
        book = await self._find_book_row(db, req)
        if book is None:
            return None
        return await catalog.resolve_survivor(db, book)

    async def _find_book_row(
        self, db: AsyncSession, req: WishlistRequest
    ) -> Book | None:
        if req.open_library_work_id:
            result = await db.execute(
                select(Book).where(
                    Book.open_library_work_id == req.open_library_work_id
                )
            )
            book = result.scalar_one_or_none()
            if book:
                return book

        if req.google_books_id:
            # uq_books_google_books_id (#613) now prevents duplicates; the
            # oldest-row LIMIT 1 (#611) stays as a harmless guard so a
            # duplicate could never cause MultipleResultsFound.
            result = await db.execute(
                select(Book)
                .where(Book.google_books_id == req.google_books_id)
                .order_by(Book.created_at, Book.id)
                .limit(1)
            )
            book = result.scalar_one_or_none()
            if book:
                return book
        return None

    async def _find_or_create_edition(
        self,
        db: AsyncSession,
        book_id: uuid.UUID,
        req: WishlistRequest,
        *,
        create: bool = False,
    ) -> Edition | None:
        """The Edition with the first edition's ISBN-13; otherwise a new one
        when ``create`` (a verified catalog token). Unsigned requests link
        only an existing Edition of ``book_id``."""
        if not req.editions:
            return None

        edition_data = req.editions[0]
        if not edition_data.isbn_13:
            return None

        result = await db.execute(
            select(Edition).where(Edition.isbn_13 == edition_data.isbn_13)
        )
        edition = result.scalar_one_or_none()
        if edition:
            # Never link a user book to another book's edition, signed or not.
            return edition if edition.book_id == book_id else None

        if not create:
            return None

        edition = Edition(
            book_id=book_id,
            isbn_13=edition_data.isbn_13,
            isbn_10=edition_data.isbn_10,
            publisher=edition_data.publisher,
            publish_year=edition_data.publish_year,
            page_count=edition_data.page_count,
            format=edition_data.format or "unknown",
        )
        try:
            async with db.begin_nested():
                db.add(edition)
                await db.flush()
        except IntegrityError:
            # Another request created this ISBN-13 first: link it if it is
            # an edition of this book.
            result = await db.execute(
                select(Edition).where(Edition.isbn_13 == edition_data.isbn_13)
            )
            existing = result.scalar_one()
            return existing if existing.book_id == book_id else None
        return edition

    async def _find_or_create_user_book(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        book_id: uuid.UUID,
        edition_id: uuid.UUID | None,
        *,
        isbn: str | None = None,
        work_id: str | None = None,
        volume_id: str | None = None,
    ) -> UserBook:
        """A new library entry; :class:`AlreadyOwnedError` when the user
        already has this book: an entry for ``book_id``, for the same Open
        Library work or Google Books volume (the scan's library check, so the
        409 matches what the scan showed), or holding this edition (``isbn``,
        normalised ISBN-13, whatever book it belongs to). One query; a
        concurrent add of the same book (``uq_user_books_user_book_live``) is
        refused the same way."""
        await self._refuse_if_owned(
            db, user_id, book_id, edition_id, isbn, work_id, volume_id
        )
        user_book = UserBook(
            user_id=user_id,
            book_id=book_id,
            edition_id=edition_id,
            status="wishlisted",
            wishlisted_at=datetime.now(UTC),
        )
        try:
            async with db.begin_nested():
                db.add(user_book)
                await db.flush()
        except IntegrityError:
            # Another request added this book between the check and the
            # insert: refuse with how the user has it now.
            await self._refuse_if_owned(
                db, user_id, book_id, edition_id, isbn, work_id, volume_id
            )
            raise
        return user_book

    async def _refuse_if_owned(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        book_id: uuid.UUID,
        edition_id: uuid.UUID | None,
        isbn: str | None,
        work_id: str | None,
        volume_id: str | None,
    ) -> None:
        held = await held_entries(
            db,
            user_id,
            book_ids={book_id},
            work_ids={work_id} if work_id else (),
            volume_ids={volume_id} if volume_id else (),
            isbns={isbn} if isbn else (),
        )
        matches = matching(
            held, book_id=book_id, work_id=work_id, volume_id=volume_id, isbn=isbn
        )
        ownership = ownership_of(isbn, matches, edition_id=edition_id)
        if ownership != NOT_OWNED:
            raise AlreadyOwnedError(ownership)

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

A user book is never linked to an Edition of a different Book, signed or
not. Existing catalog rows are never modified from a request. Creation is
idempotent per work: rows are matched by provider ID first, and a
concurrent first add of the same Open Library work (unique
``books.open_library_work_id``) or ISBN-13 (unique ``editions.isbn_13``)
re-reads and links the row the other request created instead of failing.
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
from app.schemas.user_book import WishlistRequest

logger = logging.getLogger(__name__)


class CatalogTokenInvalidError(Exception):
    """No valid catalog token and no existing Book to link to (422
    ``catalog_token_invalid``)."""


class BookNotInCatalogError(Exception):
    """The book has no provider ID, so it can never be in the shared
    catalog (422 ``book_not_in_catalog``)."""


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
        user_book = await self._find_or_create_user_book(
            db, user_id, book.id, edition.id if edition else None
        )
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
        # Try to match on work-level IDs first
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
            result = await db.execute(
                select(Book).where(Book.google_books_id == req.google_books_id)
            )
            book = result.scalar_one_or_none()
            if book:
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
        )
        try:
            async with db.begin_nested():
                db.add(book)
                await db.flush()  # get book.id without committing
        except IntegrityError:
            # Another request created this work first: link its row.
            if not req.open_library_work_id:
                raise
            result = await db.execute(
                select(Book).where(
                    Book.open_library_work_id == req.open_library_work_id
                )
            )
            return result.scalar_one()
        return book

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
    ) -> UserBook:
        result = await db.execute(
            select(UserBook).where(
                UserBook.user_id == user_id,
                UserBook.book_id == book_id,
            )
        )
        user_book = result.scalar_one_or_none()
        if user_book:
            return user_book

        user_book = UserBook(
            user_id=user_id,
            book_id=book_id,
            edition_id=edition_id,
            status="wishlisted",
            wishlisted_at=datetime.now(UTC),
        )
        db.add(user_book)
        await db.flush()
        return user_book

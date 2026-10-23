"""WishlistService — finds or creates Book + Edition, creates/retrieves UserBook.

The shared catalog (``books``, ``editions``) is written only from
server-produced provider data (#602): a request whose ``catalog_token``
verifies (the server signed exactly these fields for this user) may create
missing catalog rows; any other request may only link the user to a Book
that already exists (and an existing Edition of that Book), else it is
refused with :class:`CatalogTokenInvalidError`. Existing catalog rows are
never modified from a request, signed or not.
"""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
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


class WishlistService:
    async def add(
        self, db: AsyncSession, user_id: uuid.UUID, req: WishlistRequest
    ) -> UserBook:
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
        self, db: AsyncSession, req: WishlistRequest, *, create: bool = True
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
        book = Book(
            open_library_work_id=req.open_library_work_id,
            google_books_id=req.google_books_id,
            title=req.title,
            author=req.author,
            description=req.description,
            cover_url=req.cover_url,
            subjects=req.subjects or [],
        )
        db.add(book)
        await db.flush()  # get book.id without committing
        return book

    async def _find_or_create_edition(
        self,
        db: AsyncSession,
        book_id: uuid.UUID,
        req: WishlistRequest,
        *,
        create: bool = True,
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
            if not create and edition.book_id != book_id:
                return None
            return edition

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
        db.add(edition)
        await db.flush()
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

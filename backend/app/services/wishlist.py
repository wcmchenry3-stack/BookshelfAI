"""WishlistService — upserts Book + Edition, creates/retrieves UserBook."""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.book import Book
from app.models.edition import Edition
from app.models.user_book import UserBook
from app.schemas.user_book import WishlistRequest

logger = logging.getLogger(__name__)


class WishlistService:
    async def add(
        self, db: AsyncSession, user_id: uuid.UUID, req: WishlistRequest
    ) -> UserBook:
        book = await self._find_or_create_book(db, req)
        edition = await self._find_or_create_edition(db, book.id, req)
        user_book = await self._find_or_create_user_book(
            db, user_id, book.id, edition.id if edition else None
        )
        await db.commit()
        await db.refresh(user_book, ["book", "edition"])
        # Eagerly load book.editions for serialisation
        await db.refresh(book, ["editions"])
        return user_book

    async def _find_or_create_book(
        self, db: AsyncSession, req: WishlistRequest
    ) -> Book:
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

        # Not found — create
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
        self, db: AsyncSession, book_id: uuid.UUID, req: WishlistRequest
    ) -> Edition | None:
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
            return edition

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

"""DeduplicationService — flags books already in the user's collection."""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.book import Book
from app.models.user_book import UserBook
from app.schemas.book import EnrichedBook

logger = logging.getLogger(__name__)


class DeduplicationService:
    async def check(
        self, db: AsyncSession, user_id: str, candidates: list[EnrichedBook]
    ) -> list[EnrichedBook]:
        """Set already_in_library=True on any candidate already in the user's collection."""
        for candidate in candidates:
            candidate.already_in_library = await self._is_in_library(
                db, user_id, candidate
            )
        return candidates

    async def _is_in_library(
        self, db: AsyncSession, user_id: str, candidate: EnrichedBook
    ) -> bool:
        if candidate.open_library_work_id:
            result = await db.execute(
                select(UserBook)
                .join(Book, Book.id == UserBook.book_id)
                .where(
                    Book.open_library_work_id == candidate.open_library_work_id,
                    UserBook.user_id == user_id,
                )
                # Existence only: one row is enough.
                .limit(1)
            )
            if result.scalar_one_or_none():
                return True

        if candidate.google_books_id:
            result = await db.execute(
                select(UserBook)
                .join(Book, Book.id == UserBook.book_id)
                .where(
                    Book.google_books_id == candidate.google_books_id,
                    UserBook.user_id == user_id,
                )
                # google_books_id is not unique (#613): with duplicate Book
                # rows the user may hold several, which must not raise
                # MultipleResultsFound.
                .limit(1)
            )
            if result.scalar_one_or_none():
                return True

        return False

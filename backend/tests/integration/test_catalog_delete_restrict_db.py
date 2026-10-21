"""Integration tests: deleting shared catalog rows never removes user data.

Requires a real PostgreSQL database with migrations applied (0004) — skipped
when DATABASE_URL is dummy.
"""

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.models.book import Book
from app.models.edition import Edition
from app.models.user import User
from app.models.user_book import UserBook


async def _user_and_book(db_session, tag: str):
    user = User(email=f"{tag}@example.com", google_id=f"google_{tag}")
    book = Book(title=f"Title {tag}", author="Author")
    db_session.add_all([user, book])
    await db_session.flush()
    return user, book


@pytest.mark.integration
class TestCatalogDeleteRestrict:
    async def test_delete_book_with_user_books_is_restricted(self, db_session):
        user, book = await _user_and_book(db_session, "restrict_ub")
        db_session.add(UserBook(user_id=user.id, book_id=book.id, status="wishlisted"))
        await db_session.flush()

        with pytest.raises(IntegrityError):
            async with db_session.begin_nested():
                await db_session.execute(delete(Book).where(Book.id == book.id))

        remaining = await db_session.scalar(
            select(UserBook).where(UserBook.book_id == book.id)
        )
        assert remaining is not None

    async def test_delete_book_with_editions_is_restricted(self, db_session):
        _, book = await _user_and_book(db_session, "restrict_ed")
        db_session.add(Edition(book_id=book.id, isbn_13="9780000000511"))
        await db_session.flush()

        with pytest.raises(IntegrityError):
            async with db_session.begin_nested():
                await db_session.execute(delete(Book).where(Book.id == book.id))

        remaining = await db_session.scalar(
            select(Edition).where(Edition.book_id == book.id)
        )
        assert remaining is not None

    async def test_orm_delete_of_referenced_book_does_not_cascade(self, db_session):
        user, book = await _user_and_book(db_session, "restrict_orm")
        db_session.add(UserBook(user_id=user.id, book_id=book.id, status="wishlisted"))
        await db_session.flush()
        user_id = user.id

        with pytest.raises(IntegrityError):
            async with db_session.begin_nested():
                await db_session.delete(book)
                await db_session.flush()

        db_session.expire_all()
        remaining = await db_session.scalar(
            select(UserBook).where(UserBook.user_id == user_id)
        )
        assert remaining is not None

    async def test_delete_unreferenced_book_succeeds(self, db_session):
        _, book = await _user_and_book(db_session, "unreferenced")
        book_id = book.id

        await db_session.execute(delete(Book).where(Book.id == book_id))
        await db_session.flush()

        assert await db_session.scalar(select(Book).where(Book.id == book_id)) is None

    async def test_delete_user_still_removes_their_user_books(self, db_session):
        user, book = await _user_and_book(db_session, "user_cascade")
        db_session.add(UserBook(user_id=user.id, book_id=book.id, status="wishlisted"))
        await db_session.flush()

        await db_session.execute(delete(User).where(User.id == user.id))
        await db_session.flush()

        count = await db_session.scalar(
            select(func.count())
            .select_from(UserBook)
            .where(UserBook.book_id == book.id)
        )
        assert count == 0
        # The shared catalog row is untouched.
        assert await db_session.scalar(select(Book).where(Book.id == book.id))

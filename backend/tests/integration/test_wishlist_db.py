"""Integration tests for wishlist operations against a real database.

These tests require a running PostgreSQL instance. They are automatically
skipped when DATABASE_URL is not configured or points to the dummy URL.
"""

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.book import Book
from app.models.edition import Edition
from app.models.user import User
from app.models.user_book import UserBook


@pytest.mark.integration
class TestWishlistDb:
    async def test_add_book_creates_book_edition_user_book(self, db_session):
        """Adding a book to the wishlist creates Book, Edition, and UserBook rows."""
        user = User(
            email="alice@example.com",
            google_id="google_alice_001",
            display_name="Alice",
        )
        db_session.add(user)
        await db_session.flush()

        book = Book(
            title="Dune",
            author="Frank Herbert",
            open_library_work_id="OL45804W",
        )
        db_session.add(book)
        await db_session.flush()

        edition = Edition(
            book_id=book.id,
            isbn_13="9780441013593",
            publisher="Ace Books",
            format="paperback",
        )
        db_session.add(edition)
        await db_session.flush()

        user_book = UserBook(
            user_id=user.id,
            book_id=book.id,
            edition_id=edition.id,
            status="wishlisted",
        )
        db_session.add(user_book)
        await db_session.flush()

        assert user_book.id is not None
        assert user_book.status == "wishlisted"
        assert user_book.book_id == book.id
        assert user_book.edition_id == edition.id

    async def test_dedup_by_open_library_work_id(self, db_session):
        """Two books with the same open_library_work_id violate the unique constraint."""
        book1 = Book(
            title="Dune",
            author="Frank Herbert",
            open_library_work_id="OL45804W",
        )
        db_session.add(book1)
        await db_session.flush()

        book2 = Book(
            title="Dune (duplicate)",
            author="Frank Herbert",
            open_library_work_id="OL45804W",
        )
        db_session.add(book2)
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_user_book_unique_constraint(self, db_session):
        """A user cannot have two UserBook entries for the same book."""
        user = User(
            email="bob@example.com",
            google_id="google_bob_001",
        )
        book = Book(title="Foundation", author="Isaac Asimov")
        db_session.add_all([user, book])
        await db_session.flush()

        ub1 = UserBook(user_id=user.id, book_id=book.id, status="wishlisted")
        db_session.add(ub1)
        await db_session.flush()

        ub2 = UserBook(user_id=user.id, book_id=book.id, status="purchased")
        db_session.add(ub2)
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_cascade_delete_book_removes_editions_and_user_books(
        self, db_session
    ):
        """Deleting a Book cascades to its Editions and UserBooks."""
        user = User(email="carol@example.com", google_id="google_carol_001")
        book = Book(title="1984", author="George Orwell")
        db_session.add_all([user, book])
        await db_session.flush()

        edition = Edition(book_id=book.id, isbn_13="9780451524935", format="paperback")
        user_book = UserBook(user_id=user.id, book_id=book.id, status="read")
        db_session.add_all([edition, user_book])
        await db_session.flush()

        edition_id = edition.id
        user_book_id = user_book.id

        await db_session.delete(book)
        await db_session.flush()

        assert await db_session.get(Edition, edition_id) is None
        assert await db_session.get(UserBook, user_book_id) is None

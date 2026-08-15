"""Integration tests for UserBook status transitions and constraints.

Requires a real PostgreSQL database — skipped when DATABASE_URL is dummy.
"""

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.book import Book
from app.models.user import User
from app.models.user_book import UserBook


@pytest.mark.integration
class TestUserBookStatusTransitions:
    async def test_status_transition_wishlisted_to_purchased(self, db_session):
        user = User(email="dave@example.com", google_id="google_dave_001")
        book = Book(title="Dune", author="Frank Herbert")
        db_session.add_all([user, book])
        await db_session.flush()

        ub = UserBook(user_id=user.id, book_id=book.id, status="wishlisted")
        db_session.add(ub)
        await db_session.flush()
        assert ub.status == "wishlisted"

        ub.status = "purchased"
        await db_session.flush()
        assert ub.status == "purchased"

    async def test_status_transition_purchased_to_reading_to_read(self, db_session):
        user = User(email="eve@example.com", google_id="google_eve_001")
        book = Book(title="Foundation", author="Isaac Asimov")
        db_session.add_all([user, book])
        await db_session.flush()

        ub = UserBook(user_id=user.id, book_id=book.id, status="purchased")
        db_session.add(ub)
        await db_session.flush()

        ub.status = "reading"
        await db_session.flush()
        assert ub.status == "reading"

        ub.status = "read"
        await db_session.flush()
        assert ub.status == "read"

    async def test_invalid_status_rejected(self, db_session):
        """DB check constraint rejects invalid status values."""
        user = User(email="frank@example.com", google_id="google_frank_001")
        book = Book(title="Catch-22", author="Joseph Heller")
        db_session.add_all([user, book])
        await db_session.flush()

        ub = UserBook(user_id=user.id, book_id=book.id, status="invalid_status")
        db_session.add(ub)
        with pytest.raises(IntegrityError):  # from check constraint
            await db_session.flush()

    async def test_rating_constraint_enforced(self, db_session):
        """Ratings must be between 1 and 5."""
        user = User(email="grace@example.com", google_id="google_grace_001")
        book = Book(title="Brave New World", author="Aldous Huxley")
        db_session.add_all([user, book])
        await db_session.flush()

        ub = UserBook(user_id=user.id, book_id=book.id, status="read", rating=6)
        db_session.add(ub)
        with pytest.raises(IntegrityError):  # from check constraint
            await db_session.flush()

    async def test_two_users_same_book(self, db_session):
        """Two users can each have their own UserBook for the same Book."""
        user1 = User(email="alice2@example.com", google_id="google_alice_002")
        user2 = User(email="bob2@example.com", google_id="google_bob_002")
        book = Book(title="Dune", author="Frank Herbert")
        db_session.add_all([user1, user2, book])
        await db_session.flush()

        ub1 = UserBook(user_id=user1.id, book_id=book.id, status="wishlisted")
        ub2 = UserBook(user_id=user2.id, book_id=book.id, status="purchased")
        db_session.add_all([ub1, ub2])
        await db_session.flush()

        assert ub1.id != ub2.id
        assert ub1.book_id == ub2.book_id
        assert ub1.user_id != ub2.user_id

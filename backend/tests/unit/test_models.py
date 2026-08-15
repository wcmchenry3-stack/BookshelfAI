"""Unit tests for SQLAlchemy ORM models.

Models are pure Python classes — no DB connection needed to instantiate them.
SQLAlchemy only connects when an actual async session operation runs.
"""

import uuid

from app.models.book import Book
from app.models.edition import VALID_FORMATS, Edition
from app.models.user import User
from app.models.user_book import VALID_STATUSES, UserBook


class TestUser:
    def test_tablename(self):
        assert User.__tablename__ == "users"

    def test_instantiation_required_fields(self):
        u = User(email="test@example.com", google_id="g123")
        assert u.email == "test@example.com"
        assert u.google_id == "g123"

    def test_optional_fields_default_to_none(self):
        u = User(email="test@example.com", google_id="g123")
        assert u.display_name is None
        assert u.avatar_url is None

    def test_all_fields(self):
        u = User(
            email="test@example.com",
            google_id="g123",
            display_name="Alice",
            avatar_url="https://example.com/avatar.jpg",
        )
        assert u.display_name == "Alice"
        assert u.avatar_url == "https://example.com/avatar.jpg"


class TestBook:
    def test_tablename(self):
        assert Book.__tablename__ == "books"

    def test_instantiation_required_fields(self):
        b = Book(title="Dune", author="Frank Herbert")
        assert b.title == "Dune"
        assert b.author == "Frank Herbert"

    def test_language_can_be_set_explicitly(self):
        # SQLAlchemy column defaults (default="en") apply at INSERT time, not
        # at Python object construction — so we test explicit assignment instead.
        b = Book(title="Dune", author="Frank Herbert", language="fr")
        assert b.language == "fr"

    def test_optional_fields_default_to_none(self):
        b = Book(title="Dune", author="Frank Herbert")
        assert b.description is None
        assert b.cover_url is None
        assert b.open_library_work_id is None
        assert b.google_books_id is None
        assert b.subjects is None

    def test_with_all_fields(self):
        b = Book(
            title="Dune",
            author="Frank Herbert",
            open_library_work_id="OL12345W",
            google_books_id="gbid123",
            description="A sci-fi epic.",
            cover_url="https://books.google.com/cover.jpg",
            subjects=["science fiction", "dystopia"],
            language="en",
        )
        assert b.open_library_work_id == "OL12345W"
        assert b.subjects == ["science fiction", "dystopia"]


class TestEdition:
    def test_tablename(self):
        assert Edition.__tablename__ == "editions"

    def test_valid_formats_contains_all_expected(self):
        assert "hardcover" in VALID_FORMATS
        assert "paperback" in VALID_FORMATS
        assert "ebook" in VALID_FORMATS
        assert "audiobook" in VALID_FORMATS
        assert "unknown" in VALID_FORMATS

    def test_instantiation(self):
        book_id = uuid.uuid4()
        e = Edition(book_id=book_id, isbn_13="9780441013593", format="paperback")
        assert e.book_id == book_id
        assert e.isbn_13 == "9780441013593"
        assert e.format == "paperback"

    def test_optional_fields_default_to_none(self):
        book_id = uuid.uuid4()
        e = Edition(book_id=book_id)
        assert e.isbn_13 is None
        assert e.isbn_10 is None
        assert e.publisher is None
        assert e.publish_year is None
        assert e.page_count is None

    def test_all_formats(self):
        book_id = uuid.uuid4()
        for fmt in VALID_FORMATS:
            e = Edition(book_id=book_id, format=fmt)
            assert e.format == fmt


class TestUserBook:
    def test_tablename(self):
        assert UserBook.__tablename__ == "user_books"

    def test_valid_statuses_contains_all_expected(self):
        assert "wishlisted" in VALID_STATUSES
        assert "purchased" in VALID_STATUSES
        assert "reading" in VALID_STATUSES
        assert "read" in VALID_STATUSES

    def test_instantiation(self):
        user_id = uuid.uuid4()
        book_id = uuid.uuid4()
        ub = UserBook(user_id=user_id, book_id=book_id, status="wishlisted")
        assert ub.user_id == user_id
        assert ub.book_id == book_id
        assert ub.status == "wishlisted"

    def test_optional_fields_default_to_none(self):
        user_id = uuid.uuid4()
        book_id = uuid.uuid4()
        ub = UserBook(user_id=user_id, book_id=book_id, status="wishlisted")
        assert ub.edition_id is None
        assert ub.notes is None
        assert ub.rating is None
        assert ub.wishlisted_at is None
        assert ub.purchased_at is None
        assert ub.started_at is None
        assert ub.finished_at is None

    def test_all_statuses(self):
        user_id = uuid.uuid4()
        book_id = uuid.uuid4()
        for status in VALID_STATUSES:
            ub = UserBook(user_id=user_id, book_id=book_id, status=status)
            assert ub.status == status

    def test_with_notes_and_rating(self):
        ub = UserBook(
            user_id=uuid.uuid4(),
            book_id=uuid.uuid4(),
            status="read",
            notes="Incredible book.",
            rating=5,
        )
        assert ub.notes == "Incredible book."
        assert ub.rating == 5

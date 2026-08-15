"""Integration tests for book deduplication via database constraints.

Requires a real PostgreSQL database — skipped when DATABASE_URL is dummy.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.book import Book
from app.models.edition import Edition


@pytest.mark.integration
class TestDeduplicationDb:
    async def test_isbn_13_unique_constraint(self, db_session):
        """Two editions with the same ISBN-13 violate the unique constraint."""
        book = Book(title="Dune", author="Frank Herbert")
        db_session.add(book)
        await db_session.flush()

        ed1 = Edition(book_id=book.id, isbn_13="9780441013593", format="paperback")
        db_session.add(ed1)
        await db_session.flush()

        ed2 = Edition(book_id=book.id, isbn_13="9780441013593", format="hardcover")
        db_session.add(ed2)
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_different_editions_same_book(self, db_session):
        """Multiple editions with different ISBNs can belong to the same book."""
        book = Book(title="Dune", author="Frank Herbert")
        db_session.add(book)
        await db_session.flush()

        ed1 = Edition(book_id=book.id, isbn_13="9780441013593", format="paperback")
        ed2 = Edition(book_id=book.id, isbn_13="9780441172719", format="hardcover")
        db_session.add_all([ed1, ed2])
        await db_session.flush()

        assert ed1.id != ed2.id
        assert ed1.book_id == ed2.book_id

    async def test_find_existing_book_by_work_id(self, db_session):
        """Can query for an existing book by its Open Library work ID."""
        book = Book(
            title="Dune",
            author="Frank Herbert",
            open_library_work_id="OL45804W",
        )
        db_session.add(book)
        await db_session.flush()

        result = await db_session.execute(
            select(Book).where(Book.open_library_work_id == "OL45804W")
        )
        found = result.scalar_one_or_none()
        assert found is not None
        assert found.id == book.id
        assert found.title == "Dune"

    async def test_null_work_ids_dont_collide(self, db_session):
        """Multiple books with NULL open_library_work_id should not conflict."""
        book1 = Book(title="Unknown Book 1", author="Author A")
        book2 = Book(title="Unknown Book 2", author="Author B")
        db_session.add_all([book1, book2])
        await db_session.flush()

        assert book1.id != book2.id
        assert book1.open_library_work_id is None
        assert book2.open_library_work_id is None

    async def test_edition_format_check_constraint(self, db_session):
        """Edition format must be one of the valid values."""
        book = Book(title="Test Book", author="Test Author")
        db_session.add(book)
        await db_session.flush()

        ed = Edition(book_id=book.id, format="invalid_format")
        db_session.add(ed)
        with pytest.raises(IntegrityError):  # from check constraint
            await db_session.flush()

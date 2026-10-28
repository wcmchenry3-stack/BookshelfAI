"""#686 on real Postgres: the check agrees with the schema."""

import pytest

from app.models.book import Book
from app.models.edition import Edition
from app.services.edition_check import (
    EditionBookMismatchError,
    ensure_edition_belongs_to_book,
)


@pytest.mark.integration
class TestEditionBookCheckDb:
    async def test_pairs(self, db_session):
        a = Book(title="Dune", author="Frank Herbert")
        b = Book(title="Emma", author="Jane Austen")
        db_session.add_all([a, b])
        await db_session.flush()
        ea = Edition(book_id=a.id, isbn_13="9780441013593", format="paperback")
        eb = Edition(book_id=b.id, isbn_13="9780141439587", format="paperback")
        db_session.add_all([ea, eb])
        await db_session.flush()

        await ensure_edition_belongs_to_book(db_session, a.id, ea.id)
        await ensure_edition_belongs_to_book(db_session, b.id, eb.id)
        await ensure_edition_belongs_to_book(db_session, a.id, None)
        with pytest.raises(EditionBookMismatchError):
            await ensure_edition_belongs_to_book(db_session, a.id, eb.id)
        with pytest.raises(EditionBookMismatchError):
            await ensure_edition_belongs_to_book(db_session, b.id, ea.id)

"""A fake session for the edition/book check (#686): it knows which book
each edition belongs to and answers the check's query from that, so a query
that dropped the ``book_id`` condition would wrongly accept a mismatch."""

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

from app.models.book import Book
from app.models.edition import Edition

NOW = datetime.now(UTC)
BOOK_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
BOOK_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
ED_OF_A = uuid.UUID("a0000000-0000-0000-0000-00000000000a")
ED_OF_B = uuid.UUID("b0000000-0000-0000-0000-00000000000b")
UNKNOWN_ED = uuid.UUID("deadbeef-0000-0000-0000-000000000000")
EDITION_BOOK = {ED_OF_A: BOOK_A, ED_OF_B: BOOK_B}


def result(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    r.scalar_one.return_value = value
    return r


def user_book_row():
    book = MagicMock()
    book.id = BOOK_A
    book.title = "Dune"
    book.author = "Frank Herbert"
    book.description = None
    book.cover_url = None
    book.subjects = []
    book.language = "en"
    book.open_library_work_id = "OL45804W"
    book.google_books_id = None
    book.created_at = NOW
    book.updated_at = NOW
    book.editions = []
    ub = MagicMock()
    ub.id = uuid.uuid4()
    ub.status = "purchased"
    ub.wishlisted_at = None
    ub.purchased_at = NOW
    ub.started_at = None
    ub.finished_at = None
    ub.notes = None
    ub.rating = None
    ub.book = book
    ub.custom_book_id = None
    ub.edition = None
    ub.created_at = NOW
    ub.updated_at = NOW
    return ub


class EditionFakeSession:
    """Books A and B exist; the user has no entry yet."""

    def __init__(self):
        self.added: list = []
        self.committed = False
        self.row = user_book_row()

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        if entity is Book:
            return result(MagicMock())
        if entity is Edition:
            # Read each condition by column, so a query that binds the ids
            # to the wrong columns (or drops one) fails like on Postgres.
            bound = {c.left.key: c.right.value for c in stmt.whereclause.clauses}
            ed = bound.get("id")
            if ed in EDITION_BOOK and EDITION_BOOK[ed] == bound.get("book_id"):
                return result(ed)
            return result(None)
        # the user's existing entry (none) or the re-read for the response
        if not self.added:
            return result(None)
        return result(self.row)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        obj.id = self.row.id


def client_for(session):
    from fastapi.testclient import TestClient

    from app.auth.dependencies import get_current_user
    from app.core.database import get_db
    from app.main import app
    from app.models.user import User

    async def _db():
        yield session

    app.dependency_overrides[get_current_user] = lambda: User(
        id=uuid.UUID("00000000-0000-0000-0000-000000000686"), email="u@x.io"
    )
    app.dependency_overrides[get_db] = _db
    return TestClient(app)

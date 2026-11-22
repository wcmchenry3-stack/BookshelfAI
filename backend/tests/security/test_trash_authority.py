"""The trash (#683) is private to its owner.

Database-free (the CI security job has no Postgres): a fake session records
every statement. The same flows run on real Postgres, with two users, in
``tests/integration/test_soft_delete_trash_db.py``
(``test_restore_of_a_live_or_another_users_entry_is_404``).

- ``GET /user-books/trash``, ``POST /user-books/{id}/restore`` and
  ``DELETE /user-books/{id}`` scope every ``user_books`` query to the
  session's user, never to an id from the request; another user's entry
  (or a live one) is a 404 with nothing written.
- The restore conflict check reads only the session user's live entries.
- Restore takes no body: a ``user_id`` or ``deleted_at`` sent with it is
  ignored, and the queries stay scoped to the session's user.

Run on its own with:  pytest -m security tests/security/test_trash_authority.py
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.auth.dependencies import get_current_user
from app.core.database import get_db
from app.main import app
from app.models.user import User

pytestmark = pytest.mark.security

ALICE = User(id=uuid.UUID("00000000-0000-0000-0000-0000000683a1"), email="a@x.io")
BOBS_ENTRY = uuid.UUID("00000000-0000-0000-0000-0000000683b2")
NOW = datetime.now(UTC)


class FakeSession:
    """Answers every query with no rows, as Postgres would for another
    user's entry once the query is scoped to the session's user."""

    def __init__(self):
        self.statements: list = []
        self.committed = False

    async def execute(self, stmt, *a, **k):
        self.statements.append(stmt)
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        result.all.return_value = []
        result.scalars.return_value.all.return_value = []
        return result

    async def commit(self):
        self.committed = True

    async def rollback(self):
        pass


@pytest.fixture
def session():
    fake = FakeSession()

    async def _db():
        yield fake

    app.dependency_overrides[get_current_user] = lambda: ALICE
    app.dependency_overrides[get_db] = _db
    yield fake
    app.dependency_overrides.clear()


def _user_books_queries(statements):
    out = []
    for stmt in statements:
        compiled = stmt.compile(dialect=postgresql.dialect())
        if "user_books" in str(compiled):
            out.append(compiled)
    return out


def _scoped_to_alice(compiled) -> bool:
    return (
        "user_books.user_id = %(user_id_1)s" in str(compiled)
        and compiled.params["user_id_1"] == ALICE.id
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", f"/user-books/{BOBS_ENTRY}/restore"),
        ("delete", f"/user-books/{BOBS_ENTRY}"),
    ],
)
def test_another_users_entry_is_404_and_nothing_is_written(session, method, path):
    resp = getattr(TestClient(app), method)(path)
    assert resp.status_code == 404
    assert not session.committed
    queries = _user_books_queries(session.statements)
    assert queries, "no user_books query was made"
    assert all(_scoped_to_alice(q) for q in queries)


def test_trash_list_is_scoped_to_the_session_user(session):
    resp = TestClient(app).get("/user-books/trash")
    assert resp.status_code == 200
    assert resp.json() == []
    (query,) = _user_books_queries(session.statements)
    assert _scoped_to_alice(query)
    assert "user_books.deleted_at IS NOT NULL" in str(query)


def test_restore_takes_no_user_id_from_the_request(session):
    resp = TestClient(app).post(
        f"/user-books/{BOBS_ENTRY}/restore",
        json={"user_id": str(BOBS_ENTRY), "deleted_at": None},
    )
    assert resp.status_code == 404
    queries = _user_books_queries(session.statements)
    assert all(_scoped_to_alice(q) for q in queries)


async def test_restore_conflict_check_reads_only_the_users_live_entries():
    from app.api import user_books as user_books_api

    session = FakeSession()
    book = SimpleNamespace(id=uuid.uuid4(), merged_into_id=None)
    await user_books_api._has_live_entry_of_work(session, ALICE.id, book)
    (query,) = _user_books_queries(session.statements)
    assert _scoped_to_alice(query)
    assert "user_books.deleted_at IS NULL" in str(query)

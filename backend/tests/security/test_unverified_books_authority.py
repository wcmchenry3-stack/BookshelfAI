"""Unverified entries (#616) keep the shared catalog provider-only and stay
private to their creator.

Database-free (the CI security job has no Postgres): a fake session records
every statement and every object added. The same flows run on real Postgres
in ``tests/integration/test_unverified_books_db.py``.

- The request can carry only a title, an author and an ISBN: provider IDs,
  a cover URL, a catalog token, a user ID or a status are refused (422).
- Every field is bounded and validated; a malformed ISBN is refused.
- Creating an entry never reads or writes ``books`` / ``editions`` (so it can
  never claim an ISBN in the shared catalog or pick up another user's row),
  and every query it makes is scoped to the session's user.
- The library check that marks scan / search results and refuses
  ``POST /wishlist`` duplicates (``held_entries``) inner-joins ``books``, so
  no unverified entry (whoever's) can match a result.
- The per-user cap and the user's own duplicates are refused with nothing
  written; the endpoint keeps the write rate limit.

Run on its own with:  pytest -m security tests/security/test_unverified_books_authority.py
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from limits import parse
from sqlalchemy.dialects import postgresql

from app.core.config import settings
from app.core.limiter import limiter
from app.main import app
from app.models.user import User
from app.models.user_book import UserBook
from app.models.user_custom_book import UserCustomBook
from app.services import deduplication

pytestmark = pytest.mark.security

NOW = datetime.now(UTC)
ALICE = User(id=uuid.UUID("00000000-0000-0000-0000-0000000616a1"), email="a@x.io")
ISBN_13 = "9780306406157"

# Tables an unverified add may touch: the user's own rows only.
ALLOWED_TABLES = {"users", "user_custom_books", "user_books"}


class FakeSession:
    """Records statements and added objects. ``existing`` are the rows the
    user's ``user_custom_books`` query returns."""

    def __init__(self, existing=()):
        self.existing = list(existing)
        self.statements: list = []
        self.added: list = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, stmt):
        self.statements.append(stmt)
        result = MagicMock()
        result.all.return_value = self.existing
        return result

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True

    async def refresh(self, obj, attrs=None):
        if isinstance(obj, UserBook):
            obj.id = obj.id or uuid.uuid4()
            obj.created_at = obj.updated_at = NOW
            obj.custom_book = next(
                o for o in self.added if isinstance(o, UserCustomBook)
            )
            obj.custom_book.created_at = obj.custom_book.updated_at = NOW


def _tables(stmt) -> set[str]:
    return {t.name for t in stmt.get_final_froms()} | {
        t.name for f in stmt.get_final_froms() for t in getattr(f, "tables", [])
    }


def _row(title="Pond Notes", author="A. Local", isbn_13=None):
    return SimpleNamespace(title=title, author=author, isbn_13=isbn_13)


@pytest.fixture
def post():
    from app.auth.dependencies import get_current_user
    from app.core.database import get_db

    state = {"db": FakeSession()}

    async def _db():
        yield state["db"]

    def _post(body, *, db=None):
        if db is not None:
            state["db"] = db
        app.dependency_overrides[get_current_user] = lambda: ALICE
        app.dependency_overrides[get_db] = _db
        return TestClient(app).post("/wishlist/unverified", json=body), state["db"]

    yield _post
    app.dependency_overrides.clear()


# ── What the request may carry ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "extra",
    [
        {"open_library_work_id": "OL1W"},
        {"google_books_id": "gb1"},
        {"cover_url": "https://example.com/c.jpg"},
        {"catalog_token": "x.y"},
        {"user_id": str(uuid.uuid4())},
        {"status": "read"},
        {"editions": [{"isbn_13": ISBN_13}]},
        {"book_id": str(uuid.uuid4())},
    ],
)
def test_provider_ids_cover_and_other_fields_are_refused(post, extra):
    resp, db = post({"title": "T", "author": "A", **extra})
    assert resp.status_code == 422
    assert db.added == [] and db.statements == []


@pytest.mark.parametrize(
    "body",
    [
        {"title": "x" * 501, "author": "A"},
        {"title": "T", "author": "x" * 501},
        {"title": "   ", "author": "A"},
        {"title": "T", "author": ""},
        {"title": "T\x00", "author": "A"},
        {"title": "T", "author": "A\u0085B"},
        {"title": "T\nU", "author": "A"},
        {"title": "T", "author": "A", "isbn": "9780306406158"},  # bad check digit
        {"title": "T", "author": "A", "isbn": "0306406153"},  # bad check digit
        {"title": "T", "author": "A", "isbn": "12345"},
        {"title": "T", "author": "A", "isbn": "9" * 21},
        {"author": "A"},
        {"title": "T"},
    ],
)
def test_input_bounds(post, body):
    resp, db = post(body)
    assert resp.status_code == 422
    assert db.added == [] and db.statements == []


def test_title_and_author_at_the_limit_are_accepted(post):
    resp, _ = post({"title": "x" * 500, "author": "y" * 500})
    assert resp.status_code == 201


# ── Never the shared catalog ─────────────────────────────────────────────────


def test_create_touches_only_the_users_own_rows(post):
    resp, db = post({"title": "Pond Notes", "author": "A. Local", "isbn": "0306406152"})

    assert resp.status_code == 201
    body = resp.json()
    assert body["unverified"] is True
    assert body["unverified_isbn_13"] == ISBN_13
    assert body["book"]["open_library_work_id"] is None
    assert body["book"]["google_books_id"] is None
    assert body["book"]["cover_url"] is None
    assert body["book"]["editions"] == []
    assert body["edition"] is None

    # Only the user's own rows are written: no Book, no Edition, so no ISBN
    # is ever claimed in editions.isbn_13.
    assert {type(o) for o in db.added} == {UserCustomBook, UserBook}
    custom = next(o for o in db.added if isinstance(o, UserCustomBook))
    assert custom.user_id == ALICE.id and custom.isbn_13 == ISBN_13
    ub = next(o for o in db.added if isinstance(o, UserBook))
    assert ub.user_id == ALICE.id and ub.book_id is None and ub.edition_id is None
    assert ub.custom_book_id == custom.id

    # Every query is on the user's own tables and bound to the user's id.
    for stmt in db.statements:
        assert _tables(stmt) <= ALLOWED_TABLES, _tables(stmt)
        params = stmt.compile(dialect=postgresql.dialect()).params.values()
        assert ALICE.id in params


def test_library_check_can_never_match_an_unverified_entry():
    """``held_entries`` (scan / search marking and the POST /wishlist 409)
    inner-joins ``books`` on ``user_books.book_id``: an unverified entry has
    no ``book_id``, so it never matches a result, whoever created it."""
    captured = {}

    class Capture:
        async def execute(self, stmt):
            captured["sql"] = str(stmt.compile(dialect=postgresql.dialect()))
            result = MagicMock()
            result.all.return_value = []
            return result

    import asyncio

    asyncio.run(
        deduplication.held_entries(
            Capture(), ALICE.id, isbns={ISBN_13}, work_ids={"OL1W"}
        )
    )
    sql = captured["sql"]
    assert "JOIN books ON books.id = user_books.book_id" in sql
    assert "LEFT OUTER JOIN books" not in sql
    assert "user_custom_books" not in sql
    assert "user_books.user_id = " in sql


def test_listing_is_scoped_to_the_user():
    from app.api.user_books import _user_book_query

    stmt = _user_book_query(ALICE.id)
    params = stmt.compile(dialect=postgresql.dialect()).params.values()
    assert ALICE.id in params


# ── Duplicates, cap, rate limit ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "existing, body",
    [
        (_row(), {"title": "pond notes!", "author": " a  local "}),
        (
            _row(title="Other", isbn_13=ISBN_13),
            {"title": "T", "author": "A", "isbn": "0306406152"},
        ),
    ],
)
def test_own_duplicate_is_refused(post, existing, body):
    resp, db = post(body, db=FakeSession([existing]))
    assert resp.status_code == 409
    assert resp.json()["detail"] == "unverified_duplicate"
    assert db.added == [] and not db.committed and db.rolled_back


def test_cap_is_refused_with_nothing_written(post):
    rows = [_row(title=f"Book {i}") for i in range(3)]
    with patch.object(settings, "unverified_books_per_user_max", 3):
        resp, db = post({"title": "New", "author": "A"}, db=FakeSession(rows))
    assert resp.status_code == 409
    assert resp.json()["detail"] == "unverified_limit_reached"
    assert db.added == [] and not db.committed


def test_under_the_cap_is_accepted(post):
    rows = [_row(title=f"Book {i}") for i in range(2)]
    with patch.object(settings, "unverified_books_per_user_max", 3):
        resp, _ = post({"title": "New", "author": "A"}, db=FakeSession(rows))
    assert resp.status_code == 201


def test_default_cap_is_bounded():
    assert settings.unverified_books_per_user_max == 200


def test_endpoint_keeps_the_write_rate_limit():
    route_limits = limiter._route_limits[
        "app.api.user_books.add_unverified_to_wishlist"
    ]
    assert [str(rl.limit) for rl in route_limits] == [
        str(parse(settings.rate_limit_writes))
    ]


def test_user_is_locked_before_the_checks(post):
    """The cap and duplicate check run with the user's row locked, so
    concurrent adds by one user are serialised."""
    _, db = post({"title": "T", "author": "A"})
    first = db.statements[0]
    assert _tables(first) == {"users"}
    assert first._for_update_arg is not None

"""Soft delete, trash and restore (#683): endpoint-level checks with the
database mocked. The flows run on real Postgres in
``tests/integration/test_soft_delete_trash_db.py``.

- ``GET /user-books/trash`` and ``POST /user-books/{id}/restore`` need auth
  (401), carry the read / write rate limits, and refuse a bad UUID (422).
- ``DELETE`` sets ``deleted_at`` and deletes nothing.
- Every library query that is not the trash, restore or purge filters on
  ``deleted_at IS NULL``: the list / PATCH / purchased / restore re-read
  (``_user_book_query``), ``DELETE``'s lookup, ``POST /purchased``'s
  existing-entry lookup, ``held_entries`` (scan, search, ``POST /wishlist``)
  and the unverified cap / duplicate check.
- The trash response carries ``deleted_at`` and ``purge_after``.
"""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from limits import parse
from sqlalchemy.dialects import postgresql

from app.api import user_books as user_books_api
from app.core.config import settings
from app.core.limiter import limiter
from app.main import app
from app.models.user import User
from app.models.user_book import UserBook
from app.schemas.user_book import TrashedUserBookRead
from app.services import deduplication, trash, unverified_books

FAKE_USER = User(id=uuid.UUID("00000000-0000-0000-0000-000000000683"), email="t@x.io")
ENTRY_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbb0683")
NOW = datetime.now(UTC)


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def _live_filter_in(stmt) -> bool:
    return "user_books.deleted_at IS NULL" in _sql(stmt)


def _catalog_row(**fields):
    book = SimpleNamespace(
        id=uuid.uuid4(),
        title="Dune",
        author="Frank Herbert",
        description=None,
        cover_url=None,
        subjects=[],
        language="en",
        open_library_work_id="OL45804W",
        google_books_id=None,
        created_at=NOW,
        updated_at=NOW,
        editions=[],
        merged_into_id=None,
    )
    data = {
        "id": ENTRY_ID,
        "user_id": FAKE_USER.id,
        "book_id": book.id,
        "book": book,
        "custom_book_id": None,
        "custom_book": None,
        "edition": None,
        "status": "read",
        "wishlisted_at": NOW,
        "purchased_at": None,
        "started_at": None,
        "finished_at": None,
        "notes": "loved it",
        "rating": 5,
        "title_override": None,
        "author_override": None,
        "cover_url_override": None,
        "created_at": NOW,
        "updated_at": NOW,
        "deleted_at": None,
    }
    data.update(fields)
    return SimpleNamespace(**data)


class _Scalars(list):
    def all(self):
        return list(self)


class _Result:
    def __init__(self, rows=(), one=None):
        self._rows = list(rows)
        self._one = one

    def scalar_one_or_none(self):
        return self._one

    def scalar_one(self):
        return self._one

    def scalars(self):
        return _Scalars(self._rows)

    def all(self):
        return list(self._rows)


@pytest.fixture
def client():
    from app.auth.dependencies import get_current_user

    app.dependency_overrides[get_current_user] = lambda: FAKE_USER
    yield TestClient(app)
    app.dependency_overrides.clear()


def _with_db(db):
    from app.core.database import get_db

    async def _db():
        yield db

    app.dependency_overrides[get_db] = _db


# ── Auth, rate limits, validation ────────────────────────────────────────────


class TestAuthAndValidation:
    def test_trash_requires_auth(self):
        assert TestClient(app).get("/user-books/trash").status_code == 401

    def test_restore_requires_auth(self):
        resp = TestClient(app).post(f"/user-books/{ENTRY_ID}/restore")
        assert resp.status_code == 401

    def test_restore_rejects_a_bad_uuid(self, client):
        _with_db(AsyncMock())
        assert client.post("/user-books/not-a-uuid/restore").status_code == 422

    def test_delete_rejects_a_bad_uuid(self, client):
        _with_db(AsyncMock())
        assert client.delete("/user-books/not-a-uuid").status_code == 422

    def test_trash_has_the_read_rate_limit(self):
        limits = limiter._route_limits["app.api.user_books.list_trash"]
        assert [str(rl.limit) for rl in limits] == [
            str(parse(settings.rate_limit_reads))
        ]

    def test_restore_has_the_write_rate_limit(self):
        limits = limiter._route_limits["app.api.user_books.restore_user_book"]
        assert [str(rl.limit) for rl in limits] == [
            str(parse(settings.rate_limit_writes))
        ]

    def test_delete_keeps_the_write_rate_limit(self):
        limits = limiter._route_limits["app.api.user_books.delete_user_book"]
        assert [str(rl.limit) for rl in limits] == [
            str(parse(settings.rate_limit_writes))
        ]


# ── Delete ───────────────────────────────────────────────────────────────────


class TestDelete:
    def test_sets_deleted_at_and_deletes_nothing(self, client):
        row = _catalog_row()
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_Result(one=row))
        _with_db(db)
        resp = client.delete(f"/user-books/{ENTRY_ID}")
        assert resp.status_code == 204
        assert row.deleted_at is not None
        db.delete.assert_not_awaited()
        db.commit.assert_awaited_once()
        lock, stmt, same, evict = [c.args[0] for c in db.execute.await_args_list]
        # The user is locked first (lock order users -> user_books).
        assert lock._for_update_arg is not None
        assert "FROM users" in _sql(lock)
        # Looks up a live entry of this user only, row-locked: a trashed one
        # (or one a concurrent DELETE just trashed) is a 404.
        assert _live_filter_in(stmt)
        assert "user_books.user_id" in _sql(stmt)
        assert "FOR UPDATE OF user_books" in _sql(stmt)
        # Then, in the same transaction, the user's other trashed entries of
        # the same work (survivor or merged rows) ...
        same_sql = _sql(same)
        assert "user_books.user_id" in same_sql
        assert "user_books.deleted_at IS NOT NULL" in same_sql
        assert "user_books.id !=" in same_sql
        assert "books.merged_into_id" in same_sql
        assert "FOR UPDATE SKIP LOCKED" in same_sql
        # Only copies holding no data of the user's (owner decision,
        # 2026-10-28).
        assert _sql(UserBook.has_no_user_data()) in same_sql
        # ... and the user's trash beyond the cap, oldest last.
        evict_sql = _sql(evict)
        assert "user_books.user_id" in evict_sql
        assert "user_books.deleted_at IS NOT NULL" in evict_sql
        assert "ORDER BY user_books.deleted_at DESC, user_books.id DESC" in evict_sql
        assert "OFFSET" in evict_sql
        assert "FOR UPDATE SKIP LOCKED" in evict_sql

    def test_missing_or_already_trashed_is_404(self, client):
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_Result(one=None))
        _with_db(db)
        assert client.delete(f"/user-books/{ENTRY_ID}").status_code == 404
        db.commit.assert_not_awaited()


# ── Every library read excludes the trash ────────────────────────────────────


class TestLiveFilter:
    def test_library_query_reads_live_entries_only(self):
        assert _live_filter_in(user_books_api._user_book_query(FAKE_USER.id))

    def test_trash_query_reads_trashed_entries_only(self):
        sql = _sql(user_books_api._user_book_query(FAKE_USER.id, trashed=True))
        assert "user_books.deleted_at IS NOT NULL" in sql
        # Ties as eviction keeps them (R2-3).
        assert "ORDER BY user_books.deleted_at DESC, user_books.id DESC" in sql

    def test_list_and_patch_use_the_live_query(self, client):
        db = AsyncMock()
        _with_db(db)
        stmts = []

        async def execute(stmt, *a, **k):
            stmts.append(stmt)
            return _Result(one=None)

        db.execute = execute
        assert client.get("/user-books").status_code == 200
        resp = client.patch(f"/user-books/{ENTRY_ID}", json={"rating": 3})
        assert resp.status_code == 404
        assert len(stmts) == 2
        assert all(_live_filter_in(s) for s in stmts)

    async def test_purchased_does_not_promote_a_trashed_entry(self):
        book = SimpleNamespace(id=uuid.uuid4(), merged_into_id=None)
        stmts = []

        async def execute(stmt, *a, **k):
            stmts.append(stmt)
            if len(stmts) == 2:
                return _Result(one=book)  # the Book lookup (after the lock)
            return _Result(one=None)

        db = MagicMock()
        db.execute = execute
        db.flush = AsyncMock()
        db.commit = AsyncMock()
        with patch.object(
            user_books_api, "require_edition_of_book", AsyncMock(return_value=None)
        ):
            await user_books_api.add_purchased.__wrapped__(
                request=SimpleNamespace(),
                req=user_books_api.PurchasedCreate(book_id=book.id),
                current_user=FAKE_USER,
                db=db,
            )
        assert "FROM users" in _sql(stmts[0])  # the user lock first
        lookup = stmts[2]
        assert "user_books.book_id" in _sql(lookup)
        assert _live_filter_in(lookup)
        assert "FOR UPDATE OF user_books" in _sql(lookup)
        # A new live entry was added rather than a trashed one promoted.
        (added,) = [c.args[0] for c in db.add.call_args_list]
        assert isinstance(added, UserBook) and added.deleted_at is None

    async def test_held_entries_ignores_trashed_entries(self):
        db = MagicMock()
        db.execute = AsyncMock(return_value=_Result())
        await deduplication.held_entries(
            db, FAKE_USER.id, book_ids={uuid.uuid4()}, work_ids={"OL1W"}
        )
        (stmt,) = [c.args[0] for c in db.execute.await_args_list]
        assert _live_filter_in(stmt)

    async def test_unverified_cap_and_duplicates_count_live_entries_only(self):
        db = MagicMock()
        db.execute = AsyncMock(return_value=_Result())
        await unverified_books.refuse_if_cannot_hold(db, FAKE_USER.id, "T", "A", None)
        (stmt,) = [c.args[0] for c in db.execute.await_args_list]
        assert "user_custom_books.deleted_at IS NULL" in _sql(stmt)


# ── Trash list ───────────────────────────────────────────────────────────────


class TestTrashList:
    def test_returns_deleted_at_and_purge_after(self, client):
        deleted = NOW - timedelta(days=3)
        row = _catalog_row(deleted_at=deleted)
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_Result(rows=[row]))
        _with_db(db)
        resp = client.get("/user-books/trash")
        assert resp.status_code == 200
        (entry,) = resp.json()
        assert entry["id"] == str(ENTRY_ID)
        assert entry["notes"] == "loved it" and entry["rating"] == 5
        assert datetime.fromisoformat(entry["deleted_at"]) == deleted
        assert datetime.fromisoformat(entry["purge_after"]) == deleted + timedelta(
            days=settings.trash_retention_days
        )

    def test_purge_after_follows_the_setting(self):
        deleted = NOW
        with patch.object(settings, "trash_retention_days", 7):
            out = TrashedUserBookRead.model_validate(_catalog_row(deleted_at=deleted))
            assert out.purge_after == deleted + timedelta(days=7)


# ── Restore ──────────────────────────────────────────────────────────────────


class _RestoreDb:
    """Answers restore's queries in order: the user lock, the trashed row,
    the conflict check, the re-read."""

    def __init__(self, row, conflict=False):
        self.row = row
        self.conflict = conflict
        self.statements = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, stmt, *a, **k):
        self.statements.append(stmt)
        n = len(self.statements)
        if n == 1:
            return _Result()  # lock
        if n == 2:
            return _Result(one=self.row)
        if n == 3 and self.row is not None and self.row.custom_book is None:
            return _Result(rows=[(uuid.uuid4(),)] if self.conflict else [])
        return _Result(one=self.row)

    async def flush(self):
        pass

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


async def _restore(db):
    return await user_books_api.restore_user_book.__wrapped__(
        request=SimpleNamespace(),
        user_book_id=ENTRY_ID,
        current_user=FAKE_USER,
        db=db,
    )


class TestRestore:
    async def test_restores_the_entry_as_it_was(self):
        row = _catalog_row(deleted_at=NOW)
        db = _RestoreDb(row)
        out = await _restore(db)
        assert out is row and row.deleted_at is None
        assert (row.status, row.notes, row.rating) == ("read", "loved it", 5)
        assert db.committed
        lock, lookup, conflict, reread = db.statements
        assert lock._for_update_arg is not None
        assert "user_books.deleted_at IS NOT NULL" in _sql(lookup)
        # Row-locked against a concurrent purge.
        assert "FOR UPDATE OF user_books" in _sql(lookup)
        assert _live_filter_in(conflict)
        assert _live_filter_in(reread)

    async def test_not_in_trash_or_not_this_users_is_404(self):
        db = _RestoreDb(None)
        with pytest.raises(HTTPException) as exc:
            await _restore(db)
        assert exc.value.status_code == 404
        assert not db.committed
        assert "user_books.user_id" in _sql(db.statements[1])

    async def test_live_entry_of_the_same_work_is_409(self):
        row = _catalog_row(deleted_at=NOW)
        db = _RestoreDb(row, conflict=True)
        with pytest.raises(HTTPException) as exc:
            await _restore(db)
        assert (exc.value.status_code, exc.value.detail) == (409, "restore_conflict")
        assert row.deleted_at is not None
        assert db.rolled_back and not db.committed
        # The conflict check covers the whole merged work (#682).
        assert "books.merged_into_id" in _sql(db.statements[2])

    async def test_a_merged_row_checks_against_its_survivor(self):
        survivor = uuid.uuid4()
        row = _catalog_row(deleted_at=NOW)
        row.book.merged_into_id = survivor
        db = _RestoreDb(row, conflict=True)
        with pytest.raises(HTTPException):
            await _restore(db)
        params = db.statements[2].compile(dialect=postgresql.dialect()).params
        assert survivor in params.values()

    @pytest.mark.parametrize(
        ("error", "detail"),
        [
            (unverified_books.UnverifiedDuplicateError, "restore_conflict"),
            (unverified_books.UnverifiedLimitError, "unverified_limit_reached"),
        ],
    )
    async def test_unverified_refusals(self, error, detail):
        custom = SimpleNamespace(
            id=uuid.uuid4(),
            title="Pond",
            author="A",
            isbn_13=None,
            deleted_at=NOW,
        )
        row = _catalog_row(
            deleted_at=NOW, custom_book_id=custom.id, custom_book=custom, book=None
        )
        db = _RestoreDb(row)
        with (
            patch.object(
                user_books_api, "refuse_if_cannot_hold", AsyncMock(side_effect=error)
            ),
            pytest.raises(HTTPException) as exc,
        ):
            await _restore(db)
        assert (exc.value.status_code, exc.value.detail) == (409, detail)
        assert row.deleted_at is not None and custom.deleted_at is not None
        assert not db.committed

    async def test_unverified_restore_clears_both_rows(self):
        custom = SimpleNamespace(
            id=uuid.uuid4(), title="Pond", author="A", isbn_13=None, deleted_at=NOW
        )
        row = _catalog_row(
            deleted_at=NOW, custom_book_id=custom.id, custom_book=custom, book=None
        )
        db = _RestoreDb(row)
        with patch.object(user_books_api, "refuse_if_cannot_hold", AsyncMock()):
            await _restore(db)
        assert row.deleted_at is None and custom.deleted_at is None

    async def test_unique_index_race_is_409(self):
        from sqlalchemy.exc import IntegrityError

        row = _catalog_row(deleted_at=NOW)
        db = _RestoreDb(row)

        async def commit():
            raise IntegrityError("UPDATE", {}, Exception("uq_user_books_user_book"))

        db.commit = commit
        with pytest.raises(HTTPException) as exc:
            await _restore(db)
        assert (exc.value.status_code, exc.value.detail) == (409, "restore_conflict")
        assert db.rolled_back


# ── Purge ────────────────────────────────────────────────────────────────────


def test_purge_cutoff_follows_the_setting():
    with patch.object(settings, "trash_retention_days", 30):
        assert trash.purge_cutoff(NOW) == NOW - timedelta(days=30)


async def test_purge_skips_when_the_lock_is_held():
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one=lambda: False))
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    assert await trash.purge_trash(db) == 0
    (stmt,) = [c.args[0] for c in db.execute.await_args_list]
    assert "pg_try_advisory_xact_lock" in _sql(stmt)
    db.rollback.assert_awaited_once()


def test_documented_trash_numbers_match_the_code():
    """docs/storage-and-retention.md states these; change both together."""
    from app.core.config import Settings

    assert Settings.model_fields["trash_retention_days"].default == 30
    assert Settings.model_fields["trash_max_entries_per_user"].default == 1000
    meta = Settings.model_fields["trash_max_entries_per_user"].metadata
    bounds = {k: getattr(m, k) for m in meta for k in ("ge", "le") if hasattr(m, k)}
    assert bounds == {"ge": 100, "le": 5000}
    assert trash.PURGE_BATCH_SIZE == 500
    assert trash.PURGE_MAX_BATCHES == 20
    doc = (
        Path(__file__).resolve().parents[3] / "docs" / "storage-and-retention.md"
    ).read_text(encoding="utf-8")
    for phrase in (
        "TRASH_RETENTION_DAYS",
        "batches of 500",
        "at most 20 per run",
        "pg_try_advisory_xact_lock",
        "TRASH_MAX_ENTRIES_PER_USER",
        "newest 1,000 per user",
        "100–5,000",
        "one trashed copy per book",
        "copies with your data",
    ):
        assert phrase in doc, phrase


def test_no_doc_states_a_stale_trash_cap_or_storage_estimate():
    """R3-1 / R3-2: every "newest N" in docs/ is the cap's default, and no
    doc keeps the old per-user storage estimate."""
    import re

    from app.core.config import Settings

    cap = Settings.model_fields["trash_max_entries_per_user"].default
    docs = Path(__file__).resolve().parents[3] / "docs"
    stated = []
    for path in sorted(docs.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        name = path.relative_to(docs)
        for stale in ("newest 100", "~3 MB per user"):
            assert stale not in text, f"{name}: {stale!r}"
        for n in re.findall(r"newest (\d[\d,]*)", text):
            stated.append((name, int(n.replace(",", ""))))
    assert stated, "no doc states the trash cap"
    assert all(n == cap for _, n in stated), stated


# ── Same-book eviction (review round 2) ──────────────────────────────────────


async def test_same_book_eviction_of_an_unverified_entry_matches_the_duplicate_key():
    """Only the user's empty trashed unverified entries with the same
    normalised title and author, or the same ISBN, are deleted; the select
    is the user's own trash, other rows only, empty rows only, and takes no
    row locks (R3-3: it reads every trashed unverified entry of the user);
    the ``DELETE`` re-checks the user, the row and emptiness."""
    custom = SimpleNamespace(title="Pond", author="A. Local", isbn_13="9780306406157")
    entry = _catalog_row(book=None, book_id=None, custom_book=custom)
    same_key, same_isbn, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    rows = [
        SimpleNamespace(id=same_key, title="POND!", author="a local", isbn_13=None),
        SimpleNamespace(
            id=same_isbn, title="Other", author="B", isbn_13="9780306406157"
        ),
        SimpleNamespace(id=other, title="Other", author="A. Local", isbn_13=None),
    ]
    db = MagicMock()
    db.execute = AsyncMock(return_value=_Result(rows=rows))
    deleted = AsyncMock(return_value=2)
    with patch.object(trash, "_delete_entries", deleted):
        assert await trash.evict_same_book(db, FAKE_USER.id, entry) == 2
    (select_stmt,) = [c.args[0] for c in db.execute.await_args_list]
    sql = _sql(select_stmt)
    assert "JOIN user_custom_books" in sql
    assert "user_books.user_id" in sql and "user_books.id !=" in sql
    assert "user_books.deleted_at IS NOT NULL" in sql
    assert _sql(UserBook.has_no_user_data()) in sql
    assert "FOR UPDATE" not in sql
    _, ids, *recheck = deleted.await_args.args
    assert ids == [same_key, same_isbn]
    recheck_sql = " AND ".join(_sql(c) for c in recheck)
    assert "user_books.user_id" in recheck_sql
    assert "user_books.id !=" in recheck_sql
    assert _sql(UserBook.has_no_user_data()) in recheck_sql


# ── What counts as user data (owner decision, 2026-10-28) ────────────────────

# Columns of user_books that are not user data: keys, the book itself, and
# timestamps the server sets on its own (``wishlisted_at`` on every add).
_NOT_USER_DATA = {
    "id",
    "user_id",
    "book_id",
    "custom_book_id",
    "wishlisted_at",
    "deleted_at",
    "created_at",
    "updated_at",
}


def test_has_no_user_data_covers_every_user_set_column():
    """A new column on ``user_books`` must be either in the emptiness
    predicate or listed above as not user data, so same-book eviction never
    silently drops a copy carrying it."""
    sql = _sql(UserBook.has_no_user_data())
    columns = {c.name for c in UserBook.__table__.columns}
    user_set = columns - _NOT_USER_DATA
    assert user_set == {
        "status",
        "purchased_at",
        "started_at",
        "finished_at",
        "notes",
        "rating",
        "edition_id",
        "title_override",
        "author_override",
        "cover_url_override",
    }
    for column in user_set:
        assert f"user_books.{column}" in sql, column


def test_has_no_user_data_expression():
    from app.models.user_book import DEFAULT_STATUS

    expr = UserBook.has_no_user_data().compile(dialect=postgresql.dialect())
    sql = str(expr)
    assert "user_books.status = " in sql
    assert DEFAULT_STATUS == "wishlisted" and "wishlisted" in expr.params.values()
    for column in (
        "purchased_at",
        "started_at",
        "finished_at",
        "rating",
        "edition_id",
        "title_override",
        "author_override",
        "cover_url_override",
    ):
        assert f"user_books.{column} IS NULL" in sql, column
    # Notes NULL or whitespace only.
    assert "user_books.notes IS NULL OR user_books.notes ~ " in sql
    assert r"^\s*$" in expr.params.values()

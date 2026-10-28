"""Catalog/user split (#682), database-free.

- Override precedence: a library read shows the user's override in place of
  the catalog title / author / cover, and the catalog value otherwise; the
  ``overrides`` object says which are the user's; the catalog row is never
  changed by serialising.
- Merged rows: a read follows ``merged_into_id`` to a loaded survivor and
  never lazy-loads (a survivor that is itself merged is logged);
  ``resolve_survivor`` follows the chain and refuses a cycle or an
  over-long chain; the library check matches an entry of a merged work on
  any of the work's IDs.
- Provenance: ``source_of`` and the ``books`` row POST /wishlist creates.
- PATCH /user-books: the request schema's override semantics (absent,
  null, a value) and the 422 for an unverified entry.

Real Postgres: ``tests/integration/test_catalog_user_split_db.py``.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.models.book import BOOK_SOURCES, Book
from app.models.user import User
from app.models.user_book import UserBook
from app.schemas.user_book import UserBookRead, UserBookUpdate
from app.services import catalog
from app.services.catalog import MergeChainError, resolve_survivor, source_of

NOW = datetime.now(UTC)
ALICE = User(id=uuid.UUID("00000000-0000-0000-0000-0000000682a1"), email="a@x.io")


def _book(**fields) -> Book:
    data = {
        "id": uuid.uuid4(),
        "title": "Dune",
        "author": "Frank Herbert",
        "cover_url": "https://books.example/dune.jpg",
        "language": "en",
        "subjects": [],
        "editions": [],
        "created_at": NOW,
        "updated_at": NOW,
    }
    data.update(fields)
    return Book(**data)


def _entry(book: Book, **fields) -> UserBook:
    data = {
        "id": uuid.uuid4(),
        "user_id": ALICE.id,
        "book_id": book.id,
        "book": book,
        "status": "wishlisted",
        "created_at": NOW,
        "updated_at": NOW,
    }
    data.update(fields)
    return UserBook(**data)


# ── Override precedence ──────────────────────────────────────────────────────


class TestOverridePrecedence:
    def test_catalog_values_without_overrides(self):
        got = UserBookRead.model_validate(_entry(_book()))
        assert (got.book.title, got.book.author) == ("Dune", "Frank Herbert")
        assert got.book.cover_url == "https://books.example/dune.jpg"
        assert got.overrides.model_dump() == {
            "title": None,
            "author": None,
            "cover_url": None,
        }

    def test_each_override_wins_over_its_catalog_value(self):
        book = _book()
        got = UserBookRead.model_validate(
            _entry(
                book,
                title_override="Dune (annotated)",
                cover_url_override="https://mine.example/c.jpg",
            )
        )
        assert got.book.title == "Dune (annotated)"
        assert got.book.author == "Frank Herbert"  # not overridden
        assert got.book.cover_url == "https://mine.example/c.jpg"
        assert got.book.id == book.id
        assert got.overrides.title == "Dune (annotated)"
        assert got.overrides.author is None
        assert got.overrides.cover_url == "https://mine.example/c.jpg"

    def test_serialising_never_changes_the_catalog_row(self):
        book = _book()
        UserBookRead.model_validate(
            _entry(book, title_override="X", author_override="Y")
        )
        assert (book.title, book.author) == ("Dune", "Frank Herbert")

    def test_response_keeps_the_existing_shape(self):
        body = UserBookRead.model_validate(
            _entry(_book(), title_override="X")
        ).model_dump(mode="json")
        assert body["book"]["title"] == "X"
        assert {"id", "title", "author", "cover_url", "editions"} <= set(body["book"])
        assert body["unverified"] is False
        assert set(body["overrides"]) == {"title", "author", "cover_url"}


# ── Merged rows ──────────────────────────────────────────────────────────────


class TestMergedRead:
    def test_read_follows_a_loaded_survivor(self):
        survivor = _book(title="Dune (kept)")
        merged = _book(title="Dune (dup)", merged_into_id=survivor.id)
        merged.merged_into = survivor
        got = UserBookRead.model_validate(_entry(merged))
        assert got.book.id == survivor.id
        assert got.book.title == "Dune (kept)"

    def test_overrides_apply_on_top_of_the_survivor(self):
        survivor = _book(title="Dune (kept)")
        merged = _book(merged_into_id=survivor.id)
        merged.merged_into = survivor
        got = UserBookRead.model_validate(_entry(merged, author_override="Me"))
        assert got.book.id == survivor.id
        assert got.book.author == "Me"

    def test_unloaded_survivor_is_never_lazy_loaded(self, caplog):
        merged = _book(merged_into_id=uuid.uuid4())
        assert catalog.effective_book(merged) is merged
        assert "read without its survivor" in caplog.text

    def test_unmerged_book_is_returned_as_is(self):
        book = _book()
        assert catalog.effective_book(book) is book
        assert catalog.effective_book(None) is None

    def test_survivor_that_is_itself_merged_is_logged(self, caplog):
        """Merges keep chains at one hop; a longer one is a broken merge.
        The read shows the first hop and logs it (it never lazy-loads)."""
        end = _book(title="end")
        middle = _book(title="middle", merged_into_id=end.id)
        start = _book(title="start", merged_into_id=middle.id)
        start.merged_into = middle
        assert catalog.effective_book(start) is middle
        assert "longer than one hop" in caplog.text


class TestMergedWorkMatching:
    """#587 for a merged work: an entry matches a candidate carrying the
    entry's own IDs, the survivor's or any merged row's (scan, search and
    POST /wishlist use the same ``matching``)."""

    SURVIVOR, MERGED = uuid.uuid4(), uuid.uuid4()

    def _held(self, *, on_merged: bool):
        from types import SimpleNamespace

        from app.services.deduplication import _held

        return _held(
            SimpleNamespace(
                book_id=self.MERGED if on_merged else self.SURVIVOR,
                edition_id=None,
                open_library_work_id="OLMW" if on_merged else "OLSW",
                google_books_id=None if on_merged else "gs",
                isbn_13=None,
                isbn_10=None,
                survivor_id=self.SURVIVOR,
                family_book_ids=[self.SURVIVOR, self.MERGED],
                family_work_ids=["OLSW", "OLMW"],
                family_volume_ids=["gs", None],
            )
        )

    @pytest.mark.parametrize("on_merged", [True, False])
    @pytest.mark.parametrize(
        "candidate",
        [
            {"work_id": "OLSW"},
            {"work_id": "OLMW"},
            {"volume_id": "gs"},
            {"book_id": SURVIVOR},
            {"book_id": MERGED},
        ],
    )
    def test_any_id_of_the_work_matches(self, on_merged, candidate):
        from app.services.deduplication import matching

        held = self._held(on_merged=on_merged)
        assert held.book_id == self.SURVIVOR
        assert matching([held], **candidate) == [held]

    @pytest.mark.parametrize(
        "candidate", [{"work_id": "OLother"}, {"volume_id": "gx"}, {"isbn": "1"}]
    )
    def test_other_ids_do_not_match(self, candidate):
        from app.services.deduplication import matching

        assert matching([self._held(on_merged=True)], **candidate) == []

    def test_entry_keeps_its_own_ids(self):
        held = self._held(on_merged=True)
        assert (held.open_library_work_id, held.google_books_id) == ("OLMW", None)
        # NULLs in the aggregates (a row without that ID) are dropped.
        assert held.volume_ids == {"gs"}


class _Rows:
    """A session answering ``select(Book).where(Book.id == x)``."""

    def __init__(self, *books):
        self.by_id = {b.id: b for b in books}
        self.queries = 0

    async def execute(self, stmt):
        self.queries += 1
        result = MagicMock()
        result.scalar_one.return_value = self.by_id[stmt.whereclause.right.value]
        return result


class TestResolveSurvivor:
    async def test_unmerged_book_needs_no_query(self):
        book, db = _book(), _Rows()
        assert await resolve_survivor(db, book) is book
        assert db.queries == 0

    async def test_follows_to_the_end_of_the_chain(self):
        c = _book()
        b = _book(merged_into_id=c.id)
        a = _book(merged_into_id=b.id)
        assert await resolve_survivor(_Rows(b, c), a) is c

    async def test_cycle_is_refused(self):
        a_id, b_id = uuid.uuid4(), uuid.uuid4()
        a = _book(id=a_id, merged_into_id=b_id)
        b = _book(id=b_id, merged_into_id=a_id)
        with pytest.raises(MergeChainError):
            await resolve_survivor(_Rows(a, b), a)

    async def test_overlong_chain_is_refused(self):
        books = [_book()]
        for _ in range(catalog.MAX_MERGE_HOPS + 1):
            books.append(_book(merged_into_id=books[-1].id))
        with pytest.raises(MergeChainError):
            await resolve_survivor(_Rows(*books), books[-1])


# ── Provenance ───────────────────────────────────────────────────────────────


class TestProvenance:
    @pytest.mark.parametrize(
        "work, volume, expected",
        [
            ("OL1W", "gb1", "google_books"),
            (None, "gb1", "google_books"),
            ("OL1W", None, "open_library"),
            (None, None, "scan"),
        ],
    )
    def test_source_of(self, work, volume, expected):
        assert source_of(work, volume) == expected
        assert expected in BOOK_SOURCES

    @pytest.mark.parametrize(
        "work, volume, expected",
        [("OL682W", None, "open_library"), (None, "gb682", "google_books")],
    )
    def test_wishlist_sets_provenance_on_a_new_book(self, work, volume, expected):
        from app.core import catalog_token
        from tests.security.test_wishlist_catalog_authority import (
            FakeCatalogSession,
            _body,
        )
        from tests.security.test_wishlist_catalog_authority import (
            _book as enriched,
        )

        (signed,) = catalog_token.sign_books(
            [enriched(open_library_work_id=work, google_books_id=volume)], ALICE.id
        )
        from app.auth.dependencies import get_current_user
        from app.core.database import get_db

        db = FakeCatalogSession()

        async def _db():
            yield db

        app.dependency_overrides[get_current_user] = lambda: ALICE
        app.dependency_overrides[get_db] = _db
        try:
            before = datetime.now(UTC)
            resp = TestClient(app).post("/wishlist", json=_body(signed))
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code == 201, resp.text
        (created,) = [o for o in db.catalog_writes() if isinstance(o, Book)]
        assert created.source == expected
        assert before <= created.fetched_at <= datetime.now(UTC)
        assert created.merged_into_id is None


# ── PATCH /user-books ────────────────────────────────────────────────────────


class TestUpdateSchema:
    def test_absent_overrides_are_not_changes(self):
        assert UserBookUpdate(status="read").override_changes() == {}

    def test_null_clears_and_a_value_sets(self):
        req = UserBookUpdate(title_override=None, author_override="  Me  ")
        assert req.override_changes() == {
            "title_override": None,
            "author_override": "Me",
        }

    @pytest.mark.parametrize(
        "field, value",
        [
            ("title_override", ""),
            ("title_override", "   "),
            ("title_override", "x" * 501),
            ("author_override", "a\x00b"),
            ("author_override", "x" * 501),
            ("cover_url_override", ""),
            ("cover_url_override", "http://books.example/c.jpg"),
        ],
    )
    def test_invalid_values_are_refused(self, field, value):
        with pytest.raises(ValidationError):
            UserBookUpdate(**{field: value})

    def test_limits_are_accepted(self):
        req = UserBookUpdate(title_override="x" * 500, author_override="y" * 500)
        assert len(req.title_override) == len(req.author_override) == 500


@pytest.fixture
def patch_entry():
    """PATCH /user-books/{id} as Alice against ``entry``."""
    from app.auth.dependencies import get_current_user
    from app.core.database import get_db

    state = {}

    async def _db():
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = state["entry"]
        result.scalar_one.return_value = state["entry"]
        db.execute = AsyncMock(return_value=result)
        db.add = MagicMock()
        state["db"] = db
        yield db

    def call(entry, body):
        state["entry"] = entry
        app.dependency_overrides[get_current_user] = lambda: ALICE
        app.dependency_overrides[get_db] = _db
        return TestClient(app).patch(f"/user-books/{entry.id}", json=body), state

    yield call
    app.dependency_overrides.clear()


class TestUpdateEndpoint:
    def test_sets_and_returns_the_override(self, patch_entry):
        entry = _entry(_book())
        resp, _ = patch_entry(entry, {"title_override": "Mine"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["book"]["title"] == "Mine"
        assert resp.json()["overrides"]["title"] == "Mine"
        assert entry.title_override == "Mine"

    def test_null_resets_and_absent_keeps(self, patch_entry):
        entry = _entry(_book(), title_override="Mine", author_override="Me")
        resp, _ = patch_entry(entry, {"title_override": None})
        assert resp.status_code == 200
        assert entry.title_override is None
        assert entry.author_override == "Me"
        assert resp.json()["book"]["title"] == "Dune"

    def test_unverified_entry_is_refused_with_nothing_written(self, patch_entry):
        custom_id = uuid.uuid4()
        entry = UserBook(
            id=uuid.uuid4(),
            user_id=ALICE.id,
            custom_book_id=custom_id,
            status="wishlisted",
        )
        resp, state = patch_entry(entry, {"title_override": "X", "status": "read"})
        assert resp.status_code == 422
        assert resp.json()["detail"] == "overrides_not_supported"
        assert entry.title_override is None
        assert entry.status == "wishlisted"
        state["db"].commit.assert_not_called()

    def test_ssrf_cover_is_refused_before_the_handler(self, patch_entry):
        entry = _entry(_book())
        with patch(
            "app.core.url_validator.socket.getaddrinfo",
            return_value=[(None, None, None, None, ("169.254.169.254", 0))],
        ):
            resp, _ = patch_entry(
                entry, {"cover_url_override": "https://metadata.example/x"}
            )
        assert resp.status_code == 422
        assert entry.cover_url_override is None

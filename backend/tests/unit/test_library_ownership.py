"""Edition-level library matching (#587) — AsyncSession mocked.

``DeduplicationService`` marks scan / search results with ``ownership``;
``WishlistService`` refuses a book the user already has with
``AlreadyOwnedError`` (409). Both use one join-shaped query of the user's
library entries (``held_entries``). The same flows run on real Postgres in
``tests/integration/test_library_ownership_db.py``.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.main import app
from app.models.book import Book
from app.models.edition import Edition
from app.models.user import User
from app.schemas.book import EditionPreview, EnrichedBook
from app.schemas.user_book import WishlistRequest
from app.services.deduplication import (
    DeduplicationService,
    Held,
    candidate_key,
    ownership_of,
)
from app.services.enrichment import isbn_10_to_13, isbn_13_to_10, normalise_isbn_13
from app.services.wishlist import AlreadyOwnedError, WishlistService

USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000587")
WORK = "OL45804W"
VOLUME = "gb_dune"
DUNE_13 = "9780441013593"
DUNE_10 = "0441013597"
OTHER_13 = "9780593099322"


def _row(
    isbn_13=None, isbn_10=None, *, ol=WORK, gb=None, book_id=None, edition_id=None
):
    """A row of the library query: an entry, its Book's IDs, its edition's
    ISBNs (none: no edition)."""
    has_edition = bool(isbn_13 or isbn_10) or edition_id is not None
    return SimpleNamespace(
        book_id=book_id or uuid.uuid4(),
        edition_id=edition_id or (uuid.uuid4() if has_edition else None),
        open_library_work_id=ol,
        google_books_id=gb,
        isbn_13=isbn_13,
        isbn_10=isbn_10,
    )


def _held(isbn=None, edition_id=None):
    return Held(uuid.uuid4(), edition_id, WORK, None, isbn)


def _book(isbn_13=DUNE_13, isbn_10=None, *, ol=WORK, gb=None, editions=None):
    if editions is None:
        editions = (
            [EditionPreview(isbn_13=isbn_13, isbn_10=isbn_10)]
            if isbn_13 or isbn_10
            else []
        )
    return EnrichedBook(
        open_library_work_id=ol,
        google_books_id=gb,
        title="Dune",
        author="Frank Herbert",
        confidence=0.9,
        editions=editions,
    )


def _rows(*rows):
    result = MagicMock()
    result.all.return_value = list(rows)
    return result


def _one(value):
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _sql(db, call=-1) -> str:
    stmt = db.execute.await_args_list[call].args[0]
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


# ─── ISBN normalisation ──────────────────────────────────────────────────────


class TestIsbnNormalisation:
    def test_isbn_10_to_13(self):
        assert isbn_10_to_13(DUNE_10) == DUNE_13

    def test_round_trip_with_the_existing_13_to_10(self):
        assert isbn_10_to_13(isbn_13_to_10(OTHER_13)) == OTHER_13

    def test_x_check_digit_and_hyphens(self):
        assert isbn_10_to_13("0-8044-2957-X") == "9780804429573"

    @pytest.mark.parametrize("bad", [None, "", "123", "ABCDEFGHIJ", "044101359Z", 7])
    def test_invalid_isbn_10_is_none(self, bad):
        assert isbn_10_to_13(bad) is None

    def test_prefers_isbn_13(self):
        assert normalise_isbn_13(DUNE_13, "0000000000") == DUNE_13

    def test_falls_back_to_isbn_10(self):
        assert normalise_isbn_13(None, DUNE_10) == DUNE_13

    def test_neither(self):
        assert normalise_isbn_13(None, None) is None


class TestCandidateKey:
    def test_first_edition_is_the_one_compared(self):
        book = _book(
            editions=[EditionPreview(isbn_13=DUNE_13), EditionPreview(isbn_13=OTHER_13)]
        )
        assert candidate_key(book) == DUNE_13

    def test_isbn_10_only_edition(self):
        assert candidate_key(_book(isbn_13=None, isbn_10=DUNE_10)) == DUNE_13

    def test_no_editions(self):
        assert candidate_key(_book(editions=[])) is None


class TestOwnershipOf:
    def test_no_entries(self):
        assert ownership_of(DUNE_13, []) == "not_owned"

    def test_same_isbn(self):
        assert ownership_of(DUNE_13, [_held(DUNE_13)]) == "owned_same_edition"

    def test_same_edition_id(self):
        eid = uuid.uuid4()
        assert ownership_of(None, [_held(None, eid)], edition_id=eid) == (
            "owned_same_edition"
        )

    def test_different_isbn(self):
        assert ownership_of(DUNE_13, [_held(OTHER_13)]) == "owned_other_edition"

    @pytest.mark.parametrize(
        "isbn, entries",
        [(None, [_held(DUNE_13)]), (DUNE_13, [_held(None)]), (None, [_held(None)])],
        ids=["candidate_no_isbn", "entry_no_isbn", "neither"],
    )
    def test_no_isbn_to_compare_is_unknown(self, isbn, entries):
        assert ownership_of(isbn, entries) == "owned_edition_unknown"

    def test_every_entry_is_considered_in_any_order(self):
        """An entry without an edition never hides one with (finding 5)."""
        for entries in ([_held(None), _held(OTHER_13)], [_held(OTHER_13), _held(None)]):
            assert ownership_of(DUNE_13, entries) == "owned_other_edition"
        for entries in (
            [_held(OTHER_13), _held(DUNE_13)],
            [_held(DUNE_13), _held(None)],
        ):
            assert ownership_of(DUNE_13, entries) == "owned_same_edition"


# ─── DeduplicationService ────────────────────────────────────────────────────


def _dedup_db(*rows):
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_rows(*rows))
    return db


async def _check(db, *books):
    # A UUID (callers pass its str; both bind the same) so the SQL renders.
    return await DeduplicationService().check(db, USER_ID, list(books))


class TestDeduplicationOwnership:
    async def test_same_isbn_is_same_edition(self):
        (book,) = await _check(_dedup_db(_row(DUNE_13)), _book())
        assert book.ownership == "owned_same_edition"
        assert book.already_in_library is True and book.in_library is True

    async def test_isbn_10_candidate_matches_isbn_13_edition(self):
        (book,) = await _check(_dedup_db(_row(DUNE_13)), _book(None, DUNE_10))
        assert book.ownership == "owned_same_edition"

    async def test_isbn_13_candidate_matches_isbn_10_only_edition(self):
        (book,) = await _check(_dedup_db(_row(None, DUNE_10)), _book())
        assert book.ownership == "owned_same_edition"

    async def test_same_isbn_under_another_work(self):
        db = _dedup_db(_row(DUNE_13, ol="OL-other-W"))
        (book,) = await _check(db, _book())
        assert book.ownership == "owned_same_edition"

    async def test_work_match_with_another_isbn_is_other_edition(self):
        (book,) = await _check(_dedup_db(_row(OTHER_13)), _book())
        assert book.ownership == "owned_other_edition"
        assert book.already_in_library is True

    async def test_volume_match_with_another_isbn_is_other_edition(self):
        db = _dedup_db(_row(OTHER_13, ol=None, gb=VOLUME))
        (book,) = await _check(db, _book(gb=VOLUME))
        assert book.ownership == "owned_other_edition"

    async def test_work_entry_without_edition_and_volume_entry_with_one(self):
        """The OL-matched entry has no edition; the GB-matched one has
        another edition: both are looked at, so it is another edition
        (finding 5), whatever order the rows come in."""
        for rows in (
            [_row(), _row(OTHER_13, ol=None, gb=VOLUME)],
            [_row(OTHER_13, ol=None, gb=VOLUME), _row()],
        ):
            (book,) = await _check(_dedup_db(*rows), _book(gb=VOLUME))
            assert book.ownership == "owned_other_edition"

    async def test_entry_without_edition_is_unknown(self):
        (book,) = await _check(_dedup_db(_row()), _book())
        assert book.ownership == "owned_edition_unknown"
        assert book.in_library is True

    async def test_candidate_without_isbn_is_unknown(self):
        (book,) = await _check(_dedup_db(_row(DUNE_13)), _book(editions=[]))
        assert book.ownership == "owned_edition_unknown"

    async def test_rows_of_other_books_do_not_count(self):
        db = _dedup_db(_row(OTHER_13, ol="OL2W", gb="gb2"))
        (book,) = await _check(db, _book(gb=VOLUME))
        assert book.ownership == "not_owned"
        assert book.already_in_library is False and book.in_library is False

    async def test_one_query_for_every_result(self):
        """Finding 6: one query per response, editions joined in (no
        per-entry SELECT), whatever the number of results."""
        db = _dedup_db(_row(DUNE_13), _row(None, ol="OL3W"))
        books = [
            _book(),
            _book(OTHER_13, ol="OL2W", gb="gb2"),
            _book(None, ol="OL3W"),
        ]
        await _check(db, *books)
        assert [b.ownership for b in books] == [
            "owned_same_edition",
            "not_owned",
            "owned_edition_unknown",
        ]
        assert db.execute.await_count == 1
        sql = _sql(db)
        assert "LEFT OUTER JOIN editions" in sql
        for value in (WORK, "OL2W", "OL3W", "gb2", DUNE_13, OTHER_13, DUNE_10):
            assert value in sql
        assert USER_ID.hex in sql.replace("-", "")

    async def test_entries_with_an_edition_come_first(self):
        await _check(db := _dedup_db(), _book())
        assert "ORDER BY user_books.edition_id IS NULL" in _sql(db)

    async def test_no_query_without_ids_or_isbns(self):
        db = AsyncMock()
        (book,) = await _check(db, _book(ol=None, editions=[]))
        assert book.ownership == "not_owned"
        db.execute.assert_not_called()


# ─── WishlistService ─────────────────────────────────────────────────────────


def _request(**overrides) -> WishlistRequest:
    data = {
        "open_library_work_id": WORK,
        "title": "Dune",
        "author": "Frank Herbert",
        "editions": [EditionPreview(isbn_13=DUNE_13)],
    }
    data.update(overrides)
    return WishlistRequest(**data)


def _wishlist_db(*results):
    db = AsyncMock()
    db.add = MagicMock()
    db.begin_nested = MagicMock(return_value=MagicMock())
    db.execute = AsyncMock(side_effect=list(results))
    return db


class TestWishlistRefusesOwnedBooks:
    @pytest.fixture
    def rows(self):
        book = Book(id=uuid.uuid4(), title="Dune", author="Frank Herbert")
        edition = Edition(id=uuid.uuid4(), book_id=book.id, isbn_13=DUNE_13)
        return book, edition

    async def _refused(self, db, req) -> str:
        with pytest.raises(AlreadyOwnedError) as exc:
            await WishlistService().add(db, USER_ID, req)
        db.add.assert_not_called()
        db.commit.assert_not_called()
        db.rollback.assert_awaited()
        return exc.value.ownership

    async def test_same_edition_id(self, rows):
        book, edition = rows
        # OL lookup -> book, edition lookup -> edition, library -> entry.
        entry = _row(book_id=book.id, edition_id=edition.id)
        db = _wishlist_db(_one(book), _one(edition), _rows(entry))
        assert await self._refused(db, _request()) == "owned_same_edition"

    async def test_same_isbn_13(self, rows):
        book, _ = rows
        db = _wishlist_db(_one(book), _one(None), _rows(_row(DUNE_13)))
        assert await self._refused(db, _request()) == "owned_same_edition"

    async def test_isbn_10_request_against_isbn_13_entry(self, rows):
        book, _ = rows
        req = _request(editions=[EditionPreview(isbn_10=DUNE_10)])
        # No edition lookup without an ISBN-13.
        db = _wishlist_db(_one(book), _rows(_row(DUNE_13)))
        assert await self._refused(db, req) == "owned_same_edition"
        sql = _sql(db, 1)
        assert DUNE_13 in sql and DUNE_10 in sql

    async def test_other_edition(self, rows):
        book, edition = rows
        db = _wishlist_db(_one(book), _one(edition), _rows(_row(OTHER_13)))
        assert await self._refused(db, _request()) == "owned_other_edition"

    async def test_entry_without_edition_is_unknown(self, rows):
        book, edition = rows
        db = _wishlist_db(_one(book), _one(edition), _rows(_row(book_id=book.id)))
        assert await self._refused(db, _request()) == "owned_edition_unknown"

    async def test_request_without_isbn_is_unknown(self, rows):
        book, _ = rows
        db = _wishlist_db(_one(book), _rows(_row(DUNE_13, book_id=book.id)))
        assert await self._refused(db, _request(editions=[])) == (
            "owned_edition_unknown"
        )

    async def test_volume_match_on_another_book_row(self, rows):
        """Finding 4: the user has a Google-Books-only Book (no work ID,
        same volume); the request resolves to the work's Book. The scan
        says owned, so the add is refused too."""
        book, edition = rows
        owned = _row(OTHER_13, ol=None, gb=VOLUME)
        db = _wishlist_db(_one(book), _one(edition), _rows(owned))
        req = _request(google_books_id=VOLUME)
        assert await self._refused(db, req) == "owned_other_edition"
        sql = _sql(db, 2)
        assert f"books.google_books_id IN ('{VOLUME}')" in sql
        assert f"books.open_library_work_id IN ('{WORK}')" in sql

    async def test_library_query_is_join_shaped_and_scoped_to_the_user(self, rows):
        """Finding 1: no subquery over ``editions``; an outer join driven
        by the user's entries."""
        book, edition = rows
        db = _wishlist_db(_one(book), _one(edition), _rows())
        await WishlistService().add(db, USER_ID, _request())
        sql = _sql(db, 2)
        assert "LEFT OUTER JOIN editions ON editions.id = user_books.edition_id" in sql
        assert "SELECT editions.id" not in sql and " IN (SELECT" not in sql
        flat = sql.replace("-", "")
        assert f"user_books.user_id = '{USER_ID.hex}'" in flat
        assert f"user_books.book_id IN ('{book.id.hex}')" in flat
        assert DUNE_13 in sql and isbn_13_to_10(DUNE_13) in sql
        assert "ORDER BY user_books.edition_id IS NULL" in sql
        db.add.assert_called_once()
        db.commit.assert_awaited_once()


class TestConcurrentAdd:
    """Finding 3: two adds of the same book at once both pass the check;
    the loser's insert hits ``uq_user_books_user_book`` inside a SAVEPOINT
    and is refused like any owned book (409), not a 500."""

    @staticmethod
    def _db(book, edition, after):
        db = _wishlist_db(_one(book), _one(edition), _rows(), _rows(*after))

        async def flush():
            raise IntegrityError("INSERT", {}, Exception("uq_user_books_user_book"))

        db.flush = AsyncMock(side_effect=flush)
        return db

    async def test_loser_is_refused_with_the_ownership_code(self):
        book = Book(id=uuid.uuid4(), title="Dune", author="Frank Herbert")
        edition = Edition(id=uuid.uuid4(), book_id=book.id, isbn_13=DUNE_13)
        winner = _row(DUNE_13, book_id=book.id, edition_id=edition.id)
        db = self._db(book, edition, [winner])
        with pytest.raises(AlreadyOwnedError) as exc:
            await WishlistService().add(db, USER_ID, _request())
        assert exc.value.ownership == "owned_same_edition"
        db.begin_nested.assert_called_once()
        db.commit.assert_not_called()
        db.rollback.assert_awaited()

    async def test_other_integrity_errors_still_raise(self):
        book = Book(id=uuid.uuid4(), title="Dune", author="Frank Herbert")
        db = self._db(book, None, [])
        with pytest.raises(IntegrityError):
            await WishlistService().add(db, USER_ID, _request())


# ─── POST /wishlist 409 ──────────────────────────────────────────────────────


@pytest.fixture
def client():
    from app.auth.dependencies import get_current_user
    from app.core.database import get_db

    async def _fake_db():
        yield AsyncMock()

    app.dependency_overrides[get_current_user] = lambda: User(
        id=USER_ID, email="o@x.io"
    )
    app.dependency_overrides[get_db] = _fake_db
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestEndpoint409:
    @pytest.mark.parametrize(
        "code", ["owned_same_edition", "owned_other_edition", "owned_edition_unknown"]
    )
    def test_already_owned_is_409_with_the_code(self, client, code):
        with patch("app.api.user_books.WishlistService") as svc:
            svc.return_value.add = AsyncMock(side_effect=AlreadyOwnedError(code))
            resp = client.post(
                "/wishlist",
                json={"title": "Dune", "author": "F", "open_library_work_id": "OL1W"},
            )
        assert resp.status_code == 409
        assert resp.json() == {"detail": code}


class TestSearchMarksOwnership:
    def test_search_results_carry_ownership(self, client):
        def _mark(_db, user_id, books):
            assert user_id == str(USER_ID)
            for b in books:
                b.ownership, b.already_in_library = "owned_other_edition", True
            return books

        with (
            patch("app.api.books._gb.search_query", AsyncMock(return_value=[{}])),
            patch(
                "app.api.books._enrichment.enrich", AsyncMock(return_value=[_book()])
            ),
            patch("app.api.books.DeduplicationService") as dedup,
        ):
            dedup.return_value.check = AsyncMock(side_effect=_mark)
            resp = client.get("/books/search?q=dune")
        (book,) = resp.json()
        assert book["ownership"] == "owned_other_edition"
        assert book["already_in_library"] is True and book["in_library"] is True

    def test_failed_library_check_never_fails_the_search(self, client):
        def _mark_then_fail(_db, _user_id, books):
            # A check that marks some books before it fails.
            for b in books:
                b.ownership, b.already_in_library = "owned_same_edition", True
            raise RuntimeError("db down")

        with (
            patch("app.api.books._gb.search_query", AsyncMock(return_value=[{}])),
            patch(
                "app.api.books._enrichment.enrich", AsyncMock(return_value=[_book()])
            ),
            patch("app.api.books.DeduplicationService") as dedup,
        ):
            dedup.return_value.check = AsyncMock(side_effect=_mark_then_fail)
            resp = client.get("/books/search?q=dune")
        assert resp.status_code == 200
        (book,) = resp.json()
        # The marks the failed check left behind are reset, not sent.
        assert book["ownership"] == "not_owned"
        assert book["already_in_library"] is False and book["in_library"] is False
        assert book["catalog_token"]

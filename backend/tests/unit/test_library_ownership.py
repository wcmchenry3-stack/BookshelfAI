"""Edition-level library matching (#587) — AsyncSession mocked.

``DeduplicationService`` marks scan / search results with ``ownership``;
``WishlistService`` refuses a book the user already has with
``AlreadyOwnedError`` (409). The same flows run on real Postgres in
``tests/integration/test_library_ownership_db.py``.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.book import Book
from app.models.edition import Edition
from app.models.user import User
from app.schemas.book import EditionPreview, EnrichedBook
from app.schemas.user_book import WishlistRequest
from app.services.deduplication import DeduplicationService, candidate_key, classify
from app.services.enrichment import isbn_10_to_13, isbn_13_to_10, normalise_isbn_13
from app.services.wishlist import AlreadyOwnedError, WishlistService

USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000587")
DUNE_13 = "9780441013593"
DUNE_10 = "0441013597"
OTHER_13 = "9780593099322"


def _edition(isbn_13=None, isbn_10=None):
    return SimpleNamespace(isbn_13=isbn_13, isbn_10=isbn_10)


def _entry(edition=None, edition_id=None):
    return SimpleNamespace(edition=edition, edition_id=edition_id)


def _book(isbn_13=DUNE_13, isbn_10=None, *, ol="OL45804W", gb=None, editions=None):
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


def _scalars(rows):
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    return result


def _one(row):
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    return result


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


class TestClassify:
    def test_same_isbn(self):
        assert classify(DUNE_13, _edition(DUNE_13)) == "owned_same_edition"

    def test_isbn_10_against_isbn_13(self):
        assert classify(DUNE_13, _edition(None, DUNE_10)) == "owned_same_edition"

    def test_different_isbn(self):
        assert classify(DUNE_13, _edition(OTHER_13)) == "owned_other_edition"

    @pytest.mark.parametrize(
        "isbn, edition",
        [
            (None, _edition(DUNE_13)),
            (DUNE_13, None),
            (DUNE_13, _edition()),
            (None, None),
        ],
        ids=["candidate_no_isbn", "entry_no_edition", "edition_no_isbn", "neither"],
    )
    def test_no_isbn_on_either_side_is_unknown(self, isbn, edition):
        assert classify(isbn, edition) == "owned_edition_unknown"


# ─── DeduplicationService ────────────────────────────────────────────────────


class TestDeduplicationOwnership:
    async def test_same_isbn_is_same_edition_without_work_lookups(self):
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalars([_entry(_edition(DUNE_13))]))
        (book,) = await DeduplicationService().check(db, str(USER_ID), [_book()])
        assert book.ownership == "owned_same_edition"
        assert book.already_in_library is True and book.in_library is True
        assert db.execute.await_count == 1  # the batched ISBN query only

    async def test_isbn_10_candidate_matches_isbn_13_edition(self):
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalars([_entry(_edition(DUNE_13))]))
        (book,) = await DeduplicationService().check(
            db, str(USER_ID), [_book(isbn_13=None, isbn_10=DUNE_10)]
        )
        assert book.ownership == "owned_same_edition"

    async def test_isbn_13_candidate_matches_isbn_10_only_edition(self):
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalars([_entry(_edition(None, DUNE_10))]))
        (book,) = await DeduplicationService().check(db, str(USER_ID), [_book()])
        assert book.ownership == "owned_same_edition"

    async def test_work_match_with_another_isbn_is_other_edition(self):
        db = AsyncMock()
        db.execute = AsyncMock(
            side_effect=[_scalars([]), _one(_entry(_edition(OTHER_13)))]
        )
        (book,) = await DeduplicationService().check(db, str(USER_ID), [_book()])
        assert book.ownership == "owned_other_edition"
        assert book.already_in_library is True

    async def test_work_match_and_entry_without_edition_is_unknown(self):
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[_scalars([]), _one(_entry(None))])
        (book,) = await DeduplicationService().check(db, str(USER_ID), [_book()])
        assert book.ownership == "owned_edition_unknown"
        assert book.in_library is True

    async def test_work_match_and_candidate_without_isbn_is_unknown(self):
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_one(_entry(_edition(DUNE_13))))
        (book,) = await DeduplicationService().check(
            db, str(USER_ID), [_book(editions=[])]
        )
        assert book.ownership == "owned_edition_unknown"
        assert db.execute.await_count == 1  # no ISBN query without ISBNs

    async def test_not_in_library(self):
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[_scalars([]), _one(None), _one(None)])
        (book,) = await DeduplicationService().check(
            db, str(USER_ID), [_book(gb="gb1")]
        )
        assert book.ownership == "not_owned"
        assert book.already_in_library is False and book.in_library is False

    async def test_one_isbn_query_for_every_result(self):
        db = AsyncMock()
        db.execute = AsyncMock(
            side_effect=[_scalars([_entry(_edition(DUNE_13))]), _one(None)]
        )
        books = [_book(), _book(OTHER_13, ol="OL2W")]
        await DeduplicationService().check(db, str(USER_ID), books)
        assert [b.ownership for b in books] == ["owned_same_edition", "not_owned"]
        # 1 batched ISBN query + 1 work query for the book not matched by ISBN.
        assert db.execute.await_count == 2


# ─── WishlistService ─────────────────────────────────────────────────────────


def _request(**overrides) -> WishlistRequest:
    data = {
        "open_library_work_id": "OL45804W",
        "title": "Dune",
        "author": "Frank Herbert",
        "editions": [EditionPreview(isbn_13=DUNE_13)],
    }
    data.update(overrides)
    return WishlistRequest(**data)


def _wishlist_db(book, edition, entry):
    """OL lookup -> book, edition lookup -> edition, library lookup -> entry."""
    db = AsyncMock()
    db.add = MagicMock()
    db.begin_nested = MagicMock(return_value=MagicMock())
    db.execute = AsyncMock(side_effect=[_one(book), _one(edition), _one(entry)])
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
        entry = _entry(_edition(), edition_id=edition.id)
        db = _wishlist_db(book, edition, entry)
        assert await self._refused(db, _request()) == "owned_same_edition"

    async def test_same_isbn_13(self, rows):
        book, _ = rows
        # The posted edition is not in the catalog, the owned one has its ISBN.
        db = _wishlist_db(book, None, _entry(_edition(DUNE_13), uuid.uuid4()))
        assert await self._refused(db, _request()) == "owned_same_edition"

    async def test_isbn_10_request_against_isbn_13_entry(self, rows):
        book, _ = rows
        req = _request(editions=[EditionPreview(isbn_10=DUNE_10)])
        db = AsyncMock()
        db.add = MagicMock()
        # OL lookup -> book; no edition lookup (no ISBN-13 to look up);
        # library lookup -> the entry holding the ISBN-13 edition.
        db.execute = AsyncMock(
            side_effect=[_one(book), _one(_entry(_edition(DUNE_13), uuid.uuid4()))]
        )
        assert await self._refused(db, req) == "owned_same_edition"
        sql = str(
            db.execute.await_args_list[1]
            .args[0]
            .compile(compile_kwargs={"literal_binds": True})
        )
        assert DUNE_13 in sql and DUNE_10 in sql

    async def test_other_edition(self, rows):
        book, edition = rows
        db = _wishlist_db(book, edition, _entry(_edition(OTHER_13), uuid.uuid4()))
        assert await self._refused(db, _request()) == "owned_other_edition"

    async def test_entry_without_edition_is_unknown(self, rows):
        book, edition = rows
        db = _wishlist_db(book, edition, _entry(None))
        assert await self._refused(db, _request()) == "owned_edition_unknown"

    async def test_request_without_isbn_is_unknown(self, rows):
        book, _ = rows
        db = AsyncMock()
        db.add = MagicMock()
        # OL lookup -> book, no edition lookup (no ISBN), library -> entry.
        db.execute = AsyncMock(
            side_effect=[_one(book), _one(_entry(_edition(DUNE_13), uuid.uuid4()))]
        )
        req = _request(editions=[])
        assert await self._refused(db, req) == "owned_edition_unknown"

    async def test_library_lookup_is_scoped_to_the_user_and_isbn(self, rows):
        book, edition = rows
        db = _wishlist_db(book, edition, None)
        await WishlistService().add(db, USER_ID, _request())
        stmt = db.execute.await_args_list[2].args[0]
        sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert USER_ID.hex in sql.replace("-", "")
        assert DUNE_13 in sql and isbn_13_to_10(DUNE_13) in sql
        db.add.assert_called_once()
        db.commit.assert_awaited_once()


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
        with (
            patch("app.api.books._gb.search_query", AsyncMock(return_value=[{}])),
            patch(
                "app.api.books._enrichment.enrich", AsyncMock(return_value=[_book()])
            ),
            patch("app.api.books.DeduplicationService") as dedup,
        ):
            dedup.return_value.check = AsyncMock(side_effect=RuntimeError("db down"))
            resp = client.get("/books/search?q=dune")
        assert resp.status_code == 200
        (book,) = resp.json()
        assert book["ownership"] == "not_owned" and book["catalog_token"]

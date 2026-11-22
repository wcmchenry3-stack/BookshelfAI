"""WishlistService and POST /wishlist with catalog tokens (#602) — AsyncSession
mocked. Only a verified token may create ``books`` / ``editions`` rows; an
unsigned request may only link to existing ones."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.core import catalog_token
from app.main import app
from app.models.book import Book
from app.models.edition import Edition
from app.models.user import User
from app.schemas.book import EditionPreview
from app.schemas.user_book import WishlistRequest
from app.services.wishlist import (
    BookNotInCatalogError,
    CatalogTokenInvalidError,
    WishlistService,
)

USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000602")
OTHER_USER = uuid.UUID("00000000-0000-0000-0000-000000000603")


def _request(**overrides) -> WishlistRequest:
    data = {
        "open_library_work_id": "OL45804W",
        "google_books_id": "gb_dune_001",
        "title": "Dune",
        "author": "Frank Herbert",
        "subjects": ["Science fiction"],
        "editions": [EditionPreview(isbn_13="9780441013593", publish_year=1965)],
    }
    data.update(overrides)
    return WishlistRequest(**data)


def _signed(user_id=USER_ID, **overrides) -> WishlistRequest:
    req = _request(**overrides)
    req.catalog_token = catalog_token.sign(req, user_id)
    return req


def _mock_db(*results):
    db = AsyncMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    # A SAVEPOINT around each catalog insert (#602 race handling).
    db.begin_nested = MagicMock(return_value=MagicMock())
    rows = []
    for value in results:
        result = MagicMock()
        result.scalar_one_or_none.return_value = value
        rows.append(result)
    db.execute = AsyncMock(side_effect=rows)
    return db


def _added(db, cls):
    return [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], cls)]


class TestUnsigned:
    @pytest.mark.parametrize(
        "token", [None, "", "garbage", "e30.AAAA"], ids=["none", "empty", "bad", "{}"]
    )
    async def test_no_existing_book_is_refused_without_writes(self, token):
        # OL lookup, GB lookup: nothing.
        db = _mock_db(None, None)
        with pytest.raises(CatalogTokenInvalidError):
            await WishlistService().add(db, USER_ID, _request(catalog_token=token))
        db.add.assert_not_called()
        db.flush.assert_not_called()
        db.commit.assert_not_called()

    async def test_another_users_token_is_refused(self):
        db = _mock_db(None, None)
        with pytest.raises(CatalogTokenInvalidError):
            await WishlistService().add(db, USER_ID, _signed(user_id=OTHER_USER))
        db.add.assert_not_called()

    async def test_tampered_signed_request_is_refused(self):
        req = _signed()
        req.title = "Dune (forged)"
        db = _mock_db(None, None)
        with pytest.raises(CatalogTokenInvalidError):
            await WishlistService().add(db, USER_ID, req)
        db.add.assert_not_called()

    async def test_existing_book_and_edition_are_linked_not_changed(self):
        book = Book(id=uuid.uuid4(), title="Dune", author="Frank Herbert")
        edition = Edition(id=uuid.uuid4(), book_id=book.id, isbn_13="9780441013593")
        # OL lookup -> book, edition lookup -> edition, user_book -> none
        db = _mock_db(book, edition, None)
        req = _request(title="Forged", description="Forged", cover_url=None)
        await WishlistService().add(db, USER_ID, req)
        assert _added(db, Book) == [] and _added(db, Edition) == []
        assert book.title == "Dune" and book.description is None
        (user_book,) = db.add.call_args_list
        assert user_book.args[0].book_id == book.id
        assert user_book.args[0].edition_id == edition.id

    async def test_edition_of_another_book_is_not_linked(self):
        service = WishlistService()
        edition = Edition(id=uuid.uuid4(), book_id=uuid.uuid4(), isbn_13="978")
        db = _mock_db(edition)
        found = await service._find_or_create_edition(
            db, uuid.uuid4(), _request(), create=False
        )
        assert found is None

    async def test_missing_edition_is_not_created(self):
        db = _mock_db(None)
        found = await WishlistService()._find_or_create_edition(
            db, uuid.uuid4(), _request(), create=False
        )
        assert found is None
        db.add.assert_not_called()


class TestSigned:
    async def test_creates_book_and_edition_from_signed_fields(self):
        # OL lookup, GB lookup, edition lookup, user_book lookup: nothing.
        db = _mock_db(None, None, None, None)
        await WishlistService().add(db, USER_ID, _signed())
        (book,) = _added(db, Book)
        (edition,) = _added(db, Edition)
        assert (book.title, book.open_library_work_id) == ("Dune", "OL45804W")
        assert edition.isbn_13 == "9780441013593"

    async def test_existing_edition_is_linked_not_overwritten(self):
        book = Book(id=uuid.uuid4(), title="Dune", author="Frank Herbert")
        edition = Edition(
            id=uuid.uuid4(), book_id=book.id, isbn_13="9780441013593", publisher="Ace"
        )
        db = _mock_db(book, edition, None)
        req = _signed(
            editions=[EditionPreview(isbn_13="9780441013593", publisher="Other")]
        )
        await WishlistService().add(db, USER_ID, req)
        assert _added(db, Edition) == []
        assert edition.publisher == "Ace"

    async def test_edition_of_another_book_is_not_linked(self):
        """Signed or not, a user book never points at another book's
        edition (#602 review, finding 2)."""
        book = Book(id=uuid.uuid4(), title="Dune", author="Frank Herbert")
        other = Edition(id=uuid.uuid4(), book_id=uuid.uuid4(), isbn_13="9780441013593")
        # OL lookup -> book, edition lookup -> another book's, user_book -> none
        db = _mock_db(book, other, None)
        await WishlistService().add(db, USER_ID, _signed())
        assert _added(db, Edition) == []
        (user_book,) = db.add.call_args_list
        assert user_book.args[0].book_id == book.id
        assert user_book.args[0].edition_id is None


class TestNoProviderId:
    """A book with no work ID and no Google Books ID never creates or links
    a catalog row, even with a valid token (#602 review, finding 3)."""

    async def test_signed_book_without_ids_is_refused(self):
        db = _mock_db()
        req = _signed(open_library_work_id=None, google_books_id=None)
        assert catalog_token.check(req.catalog_token, req, USER_ID) == catalog_token.OK
        with pytest.raises(BookNotInCatalogError):
            await WishlistService().add(db, USER_ID, req)
        db.execute.assert_not_called()
        db.add.assert_not_called()
        db.commit.assert_not_called()

    async def test_replayed_token_creates_nothing(self):
        req = _signed(open_library_work_id=None, google_books_id=None)
        for _ in range(3):
            db = _mock_db()
            with pytest.raises(BookNotInCatalogError):
                await WishlistService().add(db, USER_ID, req)
            db.add.assert_not_called()

    async def test_find_or_create_never_creates_without_ids(self):
        db = _mock_db()
        req = _request(open_library_work_id=None, google_books_id=None)
        assert (
            await WishlistService()._find_or_create_book(db, req, create=True) is None
        )
        db.add.assert_not_called()


class TestRacingInserts:
    """A unique-constraint loss inside the SAVEPOINT re-reads and links the
    row the other request created (finding 3); the Postgres version is in
    tests/integration/test_wishlist_catalog_token_db.py."""

    @staticmethod
    def _flush_conflicts(db, times=1):
        calls = {"n": 0}

        async def flush():
            calls["n"] += 1
            if calls["n"] <= times:
                raise IntegrityError("INSERT", {}, Exception("duplicate key"))

        db.flush = AsyncMock(side_effect=flush)

    async def test_book_conflict_links_the_existing_work(self):
        winner = Book(id=uuid.uuid4(), title="Dune", author="Frank Herbert")
        # OL lookup, GB lookup: nothing; re-read by OL after the conflict:
        # winner.
        db = _mock_db(None, None, winner)
        self._flush_conflicts(db)
        book = await WishlistService()._find_or_create_book(db, _signed(), create=True)
        assert book is winner
        db.begin_nested.assert_called_once()

    async def test_book_conflict_without_a_work_id_links_the_volume(self):
        """#613: a Google-Books-only book loses the race on
        uq_books_google_books_id and links the winner's row (before #613
        the IntegrityError was raised: a 500)."""
        winner = Book(id=uuid.uuid4(), title="Dune", author="Frank Herbert")
        # GB lookup: nothing; re-read by GB after the conflict: winner.
        db = _mock_db(None, winner)
        self._flush_conflicts(db)
        book = await WishlistService()._find_or_create_book(
            db, _signed(open_library_work_id=None), create=True
        )
        assert book is winner
        assert _added(db, Book)[0].google_books_id == "gb_dune_001"

    async def test_book_conflict_on_the_volume_with_a_new_work_id(self):
        """#613: the work ID is new but the Google Books volume was just
        created by another request: the re-read falls through to the
        volume and links that row."""
        winner = Book(id=uuid.uuid4(), title="Dune", author="Frank Herbert")
        # OL, GB lookups: nothing; re-read: OL nothing, GB winner.
        db = _mock_db(None, None, None, winner)
        self._flush_conflicts(db)
        book = await WishlistService()._find_or_create_book(db, _signed(), create=True)
        assert book is winner
        assert db.execute.await_count == 4

    async def test_book_conflict_with_nothing_to_link_is_raised(self):
        """An IntegrityError the re-read cannot explain is not swallowed."""
        db = _mock_db(None, None, None, None)
        self._flush_conflicts(db)
        with pytest.raises(IntegrityError):
            await WishlistService()._find_or_create_book(db, _signed(), create=True)

    @pytest.mark.parametrize("same_book", [True, False])
    async def test_edition_conflict_links_only_an_edition_of_this_book(self, same_book):
        book_id = uuid.uuid4()
        winner = Edition(
            id=uuid.uuid4(),
            book_id=book_id if same_book else uuid.uuid4(),
            isbn_13="9780441013593",
        )
        result = MagicMock()
        result.scalar_one.return_value = winner
        db = _mock_db(None)
        db.execute = AsyncMock(side_effect=[*db.execute.side_effect, result])
        self._flush_conflicts(db)
        edition = await WishlistService()._find_or_create_edition(
            db, book_id, _signed(), create=True
        )
        assert edition is (winner if same_book else None)


# ─── POST /wishlist ────────────────────────────────────────────────────────────

FAKE_USER = User(id=USER_ID, email="test@example.com")


@pytest.fixture
def client():
    from app.auth.dependencies import get_current_user
    from app.core.database import get_db

    async def _fake_db():
        yield AsyncMock()

    app.dependency_overrides[get_current_user] = lambda: FAKE_USER
    app.dependency_overrides[get_db] = _fake_db
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestEndpoint:
    def test_unsigned_new_book_is_422_catalog_token_invalid(self, client):
        with patch("app.api.user_books.WishlistService") as svc:
            svc.return_value.add = AsyncMock(side_effect=CatalogTokenInvalidError)
            resp = client.post(
                "/wishlist", json={"title": "Dune", "author": "Frank Herbert"}
            )
        assert resp.status_code == 422
        assert resp.json() == {"detail": "catalog_token_invalid"}

    def test_book_without_ids_is_422_book_not_in_catalog(self, client):
        # The real service: refused before any database access.
        resp = client.post(
            "/wishlist", json={"title": "Dune", "author": "Frank Herbert"}
        )
        assert resp.status_code == 422
        assert resp.json() == {"detail": "book_not_in_catalog"}

    def test_book_not_in_catalog_error_maps_to_422(self, client):
        with patch("app.api.user_books.WishlistService") as svc:
            svc.return_value.add = AsyncMock(side_effect=BookNotInCatalogError)
            resp = client.post(
                "/wishlist",
                json={"title": "Dune", "author": "F", "google_books_id": "gb1"},
            )
        assert resp.status_code == 422
        assert resp.json() == {"detail": "book_not_in_catalog"}

    def test_token_is_passed_to_the_service(self, client):
        with patch("app.api.user_books.WishlistService") as svc:
            svc.return_value.add = AsyncMock(side_effect=CatalogTokenInvalidError)
            client.post(
                "/wishlist",
                json={"title": "Dune", "author": "F", "catalog_token": "abc.def"},
            )
        _db, user_id, req = svc.return_value.add.await_args.args
        assert user_id == USER_ID and req.catalog_token == "abc.def"

    def test_overlong_token_is_rejected_by_validation(self, client):
        token = "x" * (catalog_token.CATALOG_TOKEN_MAX_LENGTH + 1)
        resp = client.post(
            "/wishlist",
            json={"title": "Dune", "author": "F", "catalog_token": token},
        )
        assert resp.status_code == 422
        assert isinstance(resp.json()["detail"], list)

"""WishlistService and POST /wishlist with catalog tokens (#602) — AsyncSession
mocked. Only a verified token may create ``books`` / ``editions`` rows; an
unsigned request may only link to existing ones."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core import catalog_token
from app.main import app
from app.models.book import Book
from app.models.edition import Edition
from app.models.user import User
from app.schemas.book import EditionPreview
from app.schemas.user_book import WishlistRequest
from app.services.wishlist import CatalogTokenInvalidError, WishlistService

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

"""Unit tests for WishlistService — AsyncSession fully mocked."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.book import Book
from app.models.edition import Edition
from app.models.user_book import UserBook
from app.schemas.book import EditionPreview
from app.schemas.user_book import WishlistRequest
from app.services.wishlist import AlreadyOwnedError, WishlistService

USER_ID = uuid.uuid4()

BASE_REQUEST = WishlistRequest(
    open_library_work_id="OL45804W",
    google_books_id="gb_dune_001",
    title="Dune",
    author="Frank Herbert",
    subjects=["Science fiction"],
    editions=[EditionPreview(isbn_13="9780441013593", publish_year=1965)],
)


def _mock_db():
    db = AsyncMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    # A SAVEPOINT around each catalog insert (#602 race handling).
    db.begin_nested = MagicMock(return_value=MagicMock())
    return db


def _execute_returning(value):
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


@pytest.fixture
def service():
    return WishlistService()


@pytest.fixture
def existing_book():
    b = MagicMock(spec=Book)
    b.id = uuid.uuid4()
    return b


@pytest.fixture
def existing_edition():
    e = MagicMock(spec=Edition)
    e.id = uuid.uuid4()
    return e


@pytest.fixture
def existing_user_book():
    ub = MagicMock(spec=UserBook)
    ub.id = uuid.uuid4()
    return ub


class TestFindOrCreateBook:
    async def test_finds_by_open_library_id(self, service, existing_book):
        db = _mock_db()
        db.execute = AsyncMock(return_value=_execute_returning(existing_book))
        result = await service._find_or_create_book(db, BASE_REQUEST)
        assert result is existing_book
        db.add.assert_not_called()

    async def test_finds_by_google_books_id_when_ol_missing(
        self, service, existing_book
    ):
        req = WishlistRequest(
            google_books_id="gb_dune_001",
            title="Dune",
            author="Frank Herbert",
        )
        db = _mock_db()
        db.execute = AsyncMock(return_value=_execute_returning(existing_book))
        result = await service._find_or_create_book(db, req)
        assert result is existing_book

    async def test_falls_through_to_gb_when_ol_returns_none(
        self, service, existing_book
    ):
        db = _mock_db()
        # First call (OL) → None, second call (GB) → book
        db.execute = AsyncMock(
            side_effect=[
                _execute_returning(None),
                _execute_returning(existing_book),
            ]
        )
        result = await service._find_or_create_book(db, BASE_REQUEST)
        assert result is existing_book
        db.add.assert_not_called()

    async def test_creates_book_when_not_found(self, service):
        db = _mock_db()
        db.execute = AsyncMock(return_value=_execute_returning(None))

        await service._find_or_create_book(db, BASE_REQUEST, create=True)

        db.add.assert_called_once()
        db.flush.assert_called_once()
        added = db.add.call_args[0][0]
        assert added.title == "Dune"
        assert added.author == "Frank Herbert"
        assert added.open_library_work_id == "OL45804W"


class TestFindOrCreateEdition:
    async def test_finds_existing_edition_by_isbn(
        self, service, existing_book, existing_edition
    ):
        # An edition of this book (#602: never another book's edition).
        existing_edition.book_id = existing_book.id
        db = _mock_db()
        db.execute = AsyncMock(return_value=_execute_returning(existing_edition))
        result = await service._find_or_create_edition(
            db, existing_book.id, BASE_REQUEST, create=True
        )
        assert result is existing_edition
        db.add.assert_not_called()

    async def test_creates_edition_when_not_found(self, service, existing_book):
        db = _mock_db()
        db.execute = AsyncMock(return_value=_execute_returning(None))
        result = await service._find_or_create_edition(
            db, existing_book.id, BASE_REQUEST, create=True
        )
        db.add.assert_called_once()
        assert result is not None

    async def test_returns_none_when_no_editions(self, service, existing_book):
        req = WishlistRequest(title="Dune", author="Frank Herbert")
        db = _mock_db()
        result = await service._find_or_create_edition(db, existing_book.id, req)
        assert result is None
        db.execute.assert_not_called()

    async def test_returns_none_when_edition_has_no_isbn(self, service, existing_book):
        req = WishlistRequest(
            title="Dune",
            author="Frank Herbert",
            editions=[EditionPreview(isbn_13=None)],
        )
        db = _mock_db()
        result = await service._find_or_create_edition(db, existing_book.id, req)
        assert result is None
        db.execute.assert_not_called()


class TestFindOrCreateUserBook:
    async def test_existing_user_book_is_refused(self, service):
        # #587: re-adding a book the user has is a 409, no longer the
        # existing row returned idempotently.
        book_id = uuid.uuid4()
        # The library query's row: the user's entry for this book, no edition.
        entry = MagicMock(
            book_id=book_id,
            edition_id=None,
            open_library_work_id=None,
            google_books_id=None,
            isbn_13=None,
            isbn_10=None,
        )
        result = MagicMock()
        result.all.return_value = [entry]
        db = _mock_db()
        db.execute = AsyncMock(return_value=result)
        with pytest.raises(AlreadyOwnedError) as exc:
            await service._find_or_create_user_book(db, USER_ID, book_id, None)
        assert exc.value.ownership == "owned_edition_unknown"
        db.add.assert_not_called()

    async def test_creates_user_book_when_not_found(self, service):
        book_id = uuid.uuid4()
        db = _mock_db()
        db.execute = AsyncMock(return_value=_execute_returning(None))
        await service._find_or_create_user_book(db, USER_ID, book_id, None)
        db.add.assert_called_once()
        db.flush.assert_called_once()
        added = db.add.call_args[0][0]
        assert added.status == "wishlisted"
        assert added.wishlisted_at is not None

    async def test_sets_edition_id_when_provided(self, service):
        book_id = uuid.uuid4()
        edition_id = uuid.uuid4()
        db = _mock_db()
        db.execute = AsyncMock(return_value=_execute_returning(None))
        await service._find_or_create_user_book(db, USER_ID, book_id, edition_id)
        added = db.add.call_args[0][0]
        assert added.edition_id == edition_id


class TestAdd:
    async def test_add_calls_commit_and_refresh(
        self, service, existing_book, existing_user_book
    ):
        db = _mock_db()

        with (
            patch.object(
                service, "_find_or_create_book", AsyncMock(return_value=existing_book)
            ),
            patch.object(
                service, "_find_or_create_edition", AsyncMock(return_value=None)
            ),
            patch.object(
                service,
                "_find_or_create_user_book",
                AsyncMock(return_value=existing_user_book),
            ),
        ):
            result = await service.add(db, USER_ID, BASE_REQUEST)

        db.commit.assert_called_once()
        assert db.refresh.call_count >= 1
        assert result is existing_user_book

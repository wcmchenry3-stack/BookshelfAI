"""Edge paths in the auth, books and user-books routers: token types the
logout must ignore, the purchased-book upgrade, and a failing library check."""

import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api import books as books_api
from app.main import app
from app.models.refresh_token import RefreshToken
from app.schemas.book import EnrichedBook
from tests.unit.test_user_books_endpoint import BOOK_ID, FAKE_USER, _make_user_book


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def _result(value):
    res = MagicMock()
    res.scalar_one_or_none.return_value = value
    res.scalar_one.return_value = value
    return res


class TestLogoutIgnoresTokensItCannotRevoke:
    @staticmethod
    def _post(payload):
        from app.core.database import get_db

        db = AsyncMock()
        db.execute = AsyncMock(return_value=_result(None))

        async def _db():
            yield db

        app.dependency_overrides[get_db] = _db
        with patch("app.api.auth.decode_token", return_value=payload):
            resp = TestClient(app).post("/auth/logout", json={"refresh_token": "tok"})
        return resp, db

    def test_access_token_is_not_looked_up(self):
        resp, db = self._post({"type": "access", "jti": "x"})
        assert resp.status_code == 204
        db.execute.assert_not_awaited()

    def test_refresh_token_without_jti_is_not_looked_up(self):
        resp, db = self._post({"type": "refresh"})
        assert resp.status_code == 204
        db.execute.assert_not_awaited()

    def test_unknown_jti_is_a_quiet_success(self):
        resp, db = self._post({"type": "refresh", "jti": "unknown"})
        assert resp.status_code == 204
        db.commit.assert_not_awaited()

    def test_already_revoked_token_is_not_rewritten(self):
        from app.core.database import get_db

        stored = MagicMock(spec=RefreshToken)
        stored.revoked_at = "earlier"
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_result(stored))

        async def _db():
            yield db

        app.dependency_overrides[get_db] = _db
        with patch(
            "app.api.auth.decode_token", return_value={"type": "refresh", "jti": "j"}
        ):
            resp = TestClient(app).post("/auth/logout", json={"refresh_token": "t"})
        assert resp.status_code == 204
        assert stored.revoked_at == "earlier"
        db.commit.assert_not_awaited()


class TestAddPurchased:
    @staticmethod
    def _client(db):
        from app.auth.dependencies import get_current_user
        from app.core.database import get_db

        async def _db():
            yield db

        app.dependency_overrides[get_current_user] = lambda: FAKE_USER
        app.dependency_overrides[get_db] = _db
        return TestClient(app)

    def test_existing_entry_is_upgraded_and_linked_to_the_isbn_edition(self):
        edition_id = uuid.uuid4()
        existing = MagicMock(status="wishlisted", edition_id=None, purchased_at=None)
        returned = _make_user_book(status="purchased")
        db = AsyncMock()
        db.add = MagicMock()
        db.execute = AsyncMock(
            side_effect=[
                _result(MagicMock()),  # the book exists
                _result(MagicMock(id=edition_id)),  # edition for the ISBN
                _result(existing),  # the user's wishlist entry
                _result(returned),  # the re-read for the response
            ]
        )
        resp = self._client(db).post(
            "/purchased", json={"book_id": str(BOOK_ID), "isbn_13": "9780441013593"}
        )
        assert resp.status_code == 201
        assert existing.status == "purchased"
        assert isinstance(existing.purchased_at, datetime)
        assert existing.edition_id == edition_id
        db.add.assert_not_called()  # updated in place, not duplicated

    def test_without_a_matching_edition_the_existing_edition_is_kept(self):
        existing = MagicMock(
            status="wishlisted", edition_id="keep-me", purchased_at=None
        )
        db = AsyncMock()
        db.add = MagicMock()
        db.execute = AsyncMock(
            side_effect=[
                _result(MagicMock()),
                _result(None),  # no edition for that ISBN
                _result(existing),
                _result(_make_user_book(status="purchased")),
            ]
        )
        resp = self._client(db).post(
            "/purchased", json={"book_id": str(BOOK_ID), "isbn_13": "9780441013593"}
        )
        assert resp.status_code == 201
        assert existing.edition_id == "keep-me"
        assert existing.status == "purchased"
        assert isinstance(existing.purchased_at, datetime)

    def test_new_entry_is_created_when_the_user_has_none(self):
        db = AsyncMock()
        db.add = MagicMock()
        db.execute = AsyncMock(
            side_effect=[
                _result(MagicMock()),  # the book exists (no ISBN: no edition lookup)
                _result(None),  # nothing on the user's shelf
                _result(_make_user_book(status="purchased")),
            ]
        )
        resp = self._client(db).post("/purchased", json={"book_id": str(BOOK_ID)})
        assert resp.status_code == 201
        created = db.add.call_args.args[0]
        assert created.status == "purchased"
        assert created.user_id == FAKE_USER.id
        assert isinstance(created.purchased_at, datetime)
        db.commit.assert_awaited_once()


class TestMarkLibrary:
    _USER = uuid.UUID("00000000-0000-0000-0000-000000000009")

    @staticmethod
    def _books():
        return [
            EnrichedBook(
                title="Dune",
                author="Frank Herbert",
                confidence=0.9,
                already_in_library=True,
                ownership="owned_same_edition",
            )
        ]

    async def test_empty_results_skip_the_check(self):
        with patch.object(books_api, "DeduplicationService") as svc:
            assert await books_api._mark_library(AsyncMock(), self._USER, []) == []
        svc.assert_not_called()

    async def test_check_failure_returns_the_books_unmarked(self, caplog):
        db = AsyncMock()
        with patch.object(books_api, "DeduplicationService") as svc:
            svc.return_value.check = AsyncMock(side_effect=RuntimeError("db gone"))
            result = await books_api._mark_library(db, self._USER, self._books())
        assert [b.title for b in result] == ["Dune"]
        assert result[0].already_in_library is False
        assert result[0].ownership == "not_owned"
        db.rollback.assert_awaited_once()
        assert "RuntimeError" in caplog.text
        assert "db gone" not in caplog.text

    async def test_failing_rollback_does_not_escape(self):
        db = AsyncMock()
        db.rollback.side_effect = RuntimeError("closed")
        with patch.object(books_api, "DeduplicationService") as svc:
            svc.return_value.check = AsyncMock(side_effect=RuntimeError)
            result = await books_api._mark_library(db, self._USER, self._books())
        assert result[0].ownership == "not_owned"

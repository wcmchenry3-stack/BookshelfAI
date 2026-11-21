"""The "Open on" preference (#913): ``users.default_tab`` on ``GET /auth/me``
and ``PATCH /users/me/preferences``.

Database-free: ``get_current_user`` is overridden with a ``User`` object and
``get_db`` with a mock session, so a test sees exactly what the endpoint
changed and that it committed. The real token path and two users are in
``tests/security/test_user_preferences_authority.py``; real Postgres (the
migration and the check constraint) in
``tests/integration/test_users_default_tab_db.py``.
"""

import uuid
from datetime import UTC, datetime
from typing import get_args
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.auth.dependencies import get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.main import app
from app.models.user import DEFAULT_TABS, User
from app.schemas.auth import DefaultTab

PATH = "/users/me/preferences"
NOW = datetime.now(UTC)


def _user(default_tab: str | None = None) -> User:
    return User(
        id=uuid.uuid4(),
        email="prefs@example.com",
        google_id="g-prefs",
        token_version=0,
        default_tab=default_tab,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture
def db():
    session = AsyncMock()
    session.commit = AsyncMock()

    async def _db():
        yield session

    app.dependency_overrides[get_db] = _db
    yield session
    app.dependency_overrides.clear()


@pytest.fixture
def signed_in(db):
    """Override the current user; yields a setter for it."""
    holder: dict[str, User] = {}

    def use(user: User) -> User:
        holder["user"] = user
        app.dependency_overrides[get_current_user] = lambda: holder["user"]
        return user

    yield use


@pytest.fixture
def client():
    return TestClient(app)


def test_schema_values_match_the_model_and_the_check_constraint():
    assert get_args(DefaultTab) == DEFAULT_TABS == ("scan", "wishlist", "my-books")


class TestGetMe:
    def test_existing_user_has_null_default_tab(self, client, signed_in):
        signed_in(_user())
        resp = client.get("/auth/me")
        assert resp.status_code == 200
        assert "default_tab" in resp.json()
        assert resp.json()["default_tab"] is None

    @pytest.mark.parametrize("tab", DEFAULT_TABS)
    def test_returns_the_stored_default_tab(self, client, signed_in, tab):
        signed_in(_user(tab))
        resp = client.get("/auth/me")
        assert resp.status_code == 200
        assert resp.json()["default_tab"] == tab

    def test_unauthenticated_is_401(self, client, db):
        assert client.get("/auth/me").status_code == 401

    def test_rate_limited_with_rate_limit_reads(self, client, signed_in):
        signed_in(_user())
        allowed = int(settings.rate_limit_reads.split("/")[0])
        for _ in range(allowed):
            assert client.get("/auth/me").status_code == 200
        assert client.get("/auth/me").status_code == 429


class TestUpdatePreferences:
    @pytest.mark.parametrize("tab", DEFAULT_TABS)
    def test_sets_each_allowed_value(self, client, signed_in, db, tab):
        user = signed_in(_user())
        resp = client.patch(PATH, json={"default_tab": tab})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"default_tab": tab}
        assert user.default_tab == tab
        db.commit.assert_awaited_once()

    def test_null_clears_back_to_scan(self, client, signed_in, db):
        user = signed_in(_user("wishlist"))
        resp = client.patch(PATH, json={"default_tab": None})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"default_tab": None}
        assert user.default_tab is None
        db.commit.assert_awaited_once()

    def test_empty_body_changes_nothing(self, client, signed_in):
        user = signed_in(_user("my-books"))
        resp = client.patch(PATH, json={})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"default_tab": "my-books"}
        assert user.default_tab == "my-books"

    def test_get_me_reflects_the_new_value(self, client, signed_in):
        signed_in(_user())
        assert client.patch(PATH, json={"default_tab": "wishlist"}).status_code == 200
        assert client.get("/auth/me").json()["default_tab"] == "wishlist"

    @pytest.mark.parametrize(
        "value", ["settings", "Scan", "my_books", "", " scan", 1, True, ["scan"], {}]
    )
    def test_invalid_value_is_422_and_changes_nothing(
        self, client, signed_in, db, value
    ):
        user = signed_in(_user("wishlist"))
        resp = client.patch(PATH, json={"default_tab": value})
        assert resp.status_code == 422
        assert user.default_tab == "wishlist"
        db.commit.assert_not_awaited()

    @pytest.mark.parametrize(
        "body",
        [
            {"default_tab": "scan", "user_id": str(uuid.uuid4())},
            {"tier": "premium"},
            {"id": str(uuid.uuid4())},
        ],
    )
    def test_unknown_fields_are_422(self, client, signed_in, db, body):
        user = signed_in(_user("wishlist"))
        resp = client.patch(PATH, json=body)
        assert resp.status_code == 422
        assert user.default_tab == "wishlist"
        db.commit.assert_not_awaited()

    def test_non_object_body_is_422(self, client, signed_in):
        signed_in(_user())
        assert client.patch(PATH, json="scan").status_code == 422

    def test_unauthenticated_is_401(self, client, db):
        resp = client.patch(PATH, json={"default_tab": "scan"})
        assert resp.status_code == 401
        db.commit.assert_not_awaited()

    def test_invalid_token_is_401(self, client, db):
        resp = client.patch(
            PATH,
            json={"default_tab": "scan"},
            headers={"Authorization": "Bearer not-a-jwt"},
        )
        assert resp.status_code == 401
        db.commit.assert_not_awaited()

    def test_touches_no_other_row(self, client, signed_in, db):
        """The user comes from the token; the endpoint never looks anyone up."""
        signed_in(_user())
        assert client.patch(PATH, json={"default_tab": "scan"}).status_code == 200
        db.execute.assert_not_called()
        db.get.assert_not_called()

    def test_rate_limited_with_rate_limit_writes(self, client, signed_in):
        signed_in(_user())
        allowed = int(settings.rate_limit_writes.split("/")[0])
        for _ in range(allowed):
            assert client.patch(PATH, json={"default_tab": "scan"}).status_code == 200
        resp = client.patch(PATH, json={"default_tab": "scan"})
        assert resp.status_code == 429

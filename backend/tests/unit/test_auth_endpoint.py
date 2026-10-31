"""Unit tests for auth endpoints — DB and Google verification mocked."""

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.user import User

FAKE_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
NOW = datetime.now(UTC)
FAKE_USER = User(
    id=FAKE_USER_ID,
    email="test@example.com",
    created_at=NOW,
    updated_at=NOW,
)

GOOGLE_CLAIMS = {
    "sub": "google-uid-123",
    "email": "test@example.com",
    "email_verified": True,
    "name": "Test User",
    "picture": "https://example.com/avatar.jpg",
}

FAKE_ACCESS = "fake.access.token"
FAKE_REFRESH = "fake.refresh.token"
FAKE_JTI = "fake-jti-uuid"


def _mock_user(uid=FAKE_USER_ID, email="test@example.com"):
    u = MagicMock()
    u.id = uid
    u.email = email
    u.display_name = "Test User"
    u.avatar_url = "https://example.com/avatar.jpg"
    u.token_version = 0
    return u


_NO_GET = object()


def _db_returning(scalar_value, user=_NO_GET):
    """A ``get_db`` override whose queries return ``scalar_value``; ``user``,
    when given, is what ``db.get`` returns."""

    async def _fake_db():
        result = MagicMock()
        result.scalar_one_or_none.return_value = scalar_value
        db = AsyncMock()
        db.execute = AsyncMock(return_value=result)
        db.refresh = AsyncMock()
        if user is not _NO_GET:
            db.get = AsyncMock(return_value=user)
        yield db

    return _fake_db


def _refresh_record(revoked_at=None):
    from app.models.refresh_token import RefreshToken

    record = MagicMock(spec=RefreshToken)
    record.revoked_at = revoked_at
    return record


@contextmanager
def _google_login(claims=GOOGLE_CLAIMS):
    """Google verification stubbed to ``claims``, no allowlist, tokens faked."""
    with (
        patch("app.api.auth.verify_google_id_token", return_value=claims),
        patch("app.api.auth.settings") as mock_settings,
        patch("app.api.auth.create_access_token", return_value=FAKE_ACCESS),
        patch(
            "app.api.auth.create_refresh_token",
            return_value=(FAKE_REFRESH, FAKE_JTI),
        ),
    ):
        mock_settings.allowed_emails_list = []
        mock_settings.jwt_expiry_hours = 24
        mock_settings.refresh_token_expiry_days = 7
        yield mock_settings


@contextmanager
def _valid_refresh():
    """A decodable refresh token for FAKE_USER_ID (tv 0), tokens faked."""
    with (
        patch(
            "app.api.auth.decode_token",
            return_value={
                "sub": str(FAKE_USER_ID),
                "type": "refresh",
                "jti": FAKE_JTI,
                "tv": 0,
            },
        ),
        patch("app.api.auth.create_access_token", return_value=FAKE_ACCESS),
        patch(
            "app.api.auth.create_refresh_token",
            return_value=(FAKE_REFRESH, FAKE_JTI),
        ),
        patch("app.api.auth.settings") as mock_settings,
    ):
        mock_settings.jwt_expiry_hours = 24
        mock_settings.refresh_token_expiry_days = 7
        yield mock_settings


@pytest.fixture
def client():
    from app.core.database import get_db

    app.dependency_overrides[get_db] = _db_returning(None)
    yield TestClient(app)
    app.dependency_overrides.clear()


# ─── POST /auth/google ─────────────────────────────────────────────────────────


class TestGoogleAuth:
    def test_returns_401_for_invalid_google_token(self, client):
        with patch(
            "app.api.auth.verify_google_id_token",
            side_effect=Exception("signature mismatch"),
        ):
            resp = client.post("/auth/google", json={"id_token": "bad"})
        assert resp.status_code == 401

    def test_auth_error_does_not_leak_exception_message(self, client):
        """Exception details must not appear in the HTTP response body (info disclosure)."""
        with patch(
            "app.api.auth.verify_google_id_token",
            side_effect=Exception("signature mismatch internal detail"),
        ):
            resp = client.post("/auth/google", json={"id_token": "bad"})
        assert resp.status_code == 401
        assert "signature mismatch internal detail" not in resp.json()["detail"]

    def test_returns_401_for_unverified_email(self, client):
        claims = {**GOOGLE_CLAIMS, "email_verified": False}
        with patch("app.api.auth.verify_google_id_token", return_value=claims):
            resp = client.post("/auth/google", json={"id_token": "token"})
        assert resp.status_code == 401
        assert "verified" in resp.json()["detail"].lower()

    def test_returns_403_when_email_not_in_allowlist(self, client):
        with (
            patch("app.api.auth.verify_google_id_token", return_value=GOOGLE_CLAIMS),
            patch("app.api.auth.settings") as mock_settings,
        ):
            mock_settings.allowed_emails_list = ["other@example.com"]
            mock_settings.jwt_expiry_hours = 24
            resp = client.post("/auth/google", json={"id_token": "token"})
        assert resp.status_code == 403
        assert "not authorized" in resp.json()["detail"].lower()

    def test_returns_200_and_tokens_for_new_user(self):
        from app.core.database import get_db

        app.dependency_overrides[get_db] = _db_returning(None)

        with _google_login():
            resp = TestClient(app).post("/auth/google", json={"id_token": "valid"})

        app.dependency_overrides.clear()
        assert resp.status_code == 200
        data = resp.json()
        assert data["access_token"] == FAKE_ACCESS
        assert data["refresh_token"] == FAKE_REFRESH
        assert data["expires_in"] == 15 * 60

    def test_returns_200_and_tokens_for_existing_user(self):
        from app.core.database import get_db

        existing = _mock_user()
        app.dependency_overrides[get_db] = _db_returning(existing)

        with _google_login():
            resp = TestClient(app).post("/auth/google", json={"id_token": "valid"})

        app.dependency_overrides.clear()
        assert resp.status_code == 200
        assert "access_token" in resp.json()

    def test_updates_display_name_for_existing_user(self):
        from app.core.database import get_db

        existing = _mock_user()
        app.dependency_overrides[get_db] = _db_returning(existing)

        claims = {**GOOGLE_CLAIMS, "name": "Updated Name", "picture": "https://new.png"}
        with _google_login(claims):
            TestClient(app).post("/auth/google", json={"id_token": "valid"})

        app.dependency_overrides.clear()
        assert existing.display_name == "Updated Name"
        assert existing.avatar_url == "https://new.png"

    def test_allowlist_empty_permits_any_email(self):
        from app.core.database import get_db

        app.dependency_overrides[get_db] = _db_returning(None)

        with _google_login():
            resp = TestClient(app).post("/auth/google", json={"id_token": "valid"})

        app.dependency_overrides.clear()
        assert resp.status_code == 200


# ─── POST /auth/refresh ────────────────────────────────────────────────────────


class TestRefresh:
    def test_returns_401_for_expired_token(self, client):
        with patch(
            "app.api.auth.decode_token",
            side_effect=jwt.ExpiredSignatureError("expired"),
        ):
            resp = client.post("/auth/refresh", json={"refresh_token": "any"})
        assert resp.status_code == 401
        assert "expired" in resp.json()["detail"].lower()

    def test_returns_401_for_invalid_token(self, client):
        with patch(
            "app.api.auth.decode_token",
            side_effect=jwt.InvalidTokenError("bad"),
        ):
            resp = client.post("/auth/refresh", json={"refresh_token": "garbage"})
        assert resp.status_code == 401

    def test_returns_401_when_access_token_used_as_refresh(self, client):
        with patch(
            "app.api.auth.decode_token",
            return_value={"sub": str(FAKE_USER_ID), "type": "access"},
        ):
            resp = client.post("/auth/refresh", json={"refresh_token": "access-token"})
        assert resp.status_code == 401
        assert "type" in resp.json()["detail"].lower()

    def test_returns_401_for_missing_jti(self, client):
        with patch(
            "app.api.auth.decode_token",
            return_value={"sub": str(FAKE_USER_ID), "type": "refresh"},  # no jti
        ):
            resp = client.post("/auth/refresh", json={"refresh_token": "old-token"})
        assert resp.status_code == 401

    def test_returns_401_for_unknown_jti(self):
        from app.core.database import get_db

        app.dependency_overrides[get_db] = _db_returning(None)

        with patch(
            "app.api.auth.decode_token",
            return_value={
                "sub": str(FAKE_USER_ID),
                "type": "refresh",
                "jti": "unknown",
            },
        ):
            resp = TestClient(app).post("/auth/refresh", json={"refresh_token": "tok"})

        app.dependency_overrides.clear()
        assert resp.status_code == 401
        assert "revoked or not found" in resp.json()["detail"].lower()

    def test_returns_401_for_revoked_token(self):
        from app.core.database import get_db

        app.dependency_overrides[get_db] = _db_returning(
            _refresh_record(revoked_at=datetime.now(UTC))
        )

        with patch(
            "app.api.auth.decode_token",
            return_value={"sub": str(FAKE_USER_ID), "type": "refresh", "jti": FAKE_JTI},
        ):
            resp = TestClient(app).post("/auth/refresh", json={"refresh_token": "tok"})

        app.dependency_overrides.clear()
        assert resp.status_code == 401
        assert "revoked" in resp.json()["detail"].lower()

    def test_returns_401_when_user_deleted(self):
        from app.core.database import get_db

        valid_record = _refresh_record()
        app.dependency_overrides[get_db] = _db_returning(valid_record, user=None)

        with patch(
            "app.api.auth.decode_token",
            return_value={"sub": str(FAKE_USER_ID), "type": "refresh", "jti": FAKE_JTI},
        ):
            resp = TestClient(app).post("/auth/refresh", json={"refresh_token": "tok"})

        app.dependency_overrides.clear()
        assert resp.status_code == 401
        assert "not found" in resp.json()["detail"].lower()

    def test_returns_new_tokens_for_valid_refresh(self):
        from app.core.database import get_db

        user = _mock_user()
        valid_record = _refresh_record()
        app.dependency_overrides[get_db] = _db_returning(valid_record, user=user)

        with _valid_refresh():
            resp = TestClient(app).post("/auth/refresh", json={"refresh_token": "tok"})

        app.dependency_overrides.clear()
        assert resp.status_code == 200
        data = resp.json()
        assert data["access_token"] == FAKE_ACCESS
        assert data["refresh_token"] == FAKE_REFRESH

    def test_rotates_token_on_valid_refresh(self):
        """Old token must be marked revoked when a new one is issued."""
        from app.core.database import get_db

        user = _mock_user()
        valid_record = _refresh_record()
        app.dependency_overrides[get_db] = _db_returning(valid_record, user=user)

        with _valid_refresh():
            TestClient(app).post("/auth/refresh", json={"refresh_token": "tok"})

        app.dependency_overrides.clear()
        assert valid_record.revoked_at is not None


# ─── POST /auth/logout ────────────────────────────────────────────────────────


class TestLogout:
    def test_returns_204_for_valid_token(self):
        from app.core.database import get_db

        active_record = _refresh_record()
        app.dependency_overrides[get_db] = _db_returning(active_record)

        with patch(
            "app.api.auth.decode_token",
            return_value={"type": "refresh", "jti": FAKE_JTI},
        ):
            resp = TestClient(app).post("/auth/logout", json={"refresh_token": "tok"})

        app.dependency_overrides.clear()
        assert resp.status_code == 204
        assert active_record.revoked_at is not None

    def test_returns_204_for_already_invalid_token(self, client):
        """Logout with an expired/garbage token should still return 204."""
        with patch(
            "app.api.auth.decode_token",
            side_effect=jwt.InvalidTokenError("expired"),
        ):
            resp = client.post("/auth/logout", json={"refresh_token": "garbage"})
        assert resp.status_code == 204

    def test_returns_204_for_already_revoked_token(self):
        from app.core.database import get_db

        app.dependency_overrides[get_db] = _db_returning(
            _refresh_record(revoked_at=datetime.now(UTC))
        )

        with patch(
            "app.api.auth.decode_token",
            return_value={"type": "refresh", "jti": FAKE_JTI},
        ):
            resp = TestClient(app).post("/auth/logout", json={"refresh_token": "tok"})

        app.dependency_overrides.clear()
        assert resp.status_code == 204


# ─── GET /auth/me ──────────────────────────────────────────────────────────────


class TestGetMe:
    def test_returns_401_without_auth(self):
        resp = TestClient(app).get("/auth/me")
        assert resp.status_code == 401

    def test_returns_current_user_email(self):
        from app.auth.dependencies import get_current_user

        app.dependency_overrides[get_current_user] = lambda: FAKE_USER
        resp = TestClient(app).get("/auth/me")
        app.dependency_overrides.clear()
        assert resp.status_code == 200
        assert resp.json()["email"] == "test@example.com"

"""Unit tests for POST /attestation/nonce and POST /attestation/register.

Services and DB are mocked; no real database or network calls.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.attestation import AttestationResult, issue_nonce
from app.main import app
from app.models.user import User

FAKE_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
FAKE_USER = User(id=FAKE_USER_ID, email="attest@example.com")

_TEST_SECRET = "test-secret-32-bytes-aaaaaaaaa"


def _fake_db_new_key():
    """Fake async DB whose execute returns no existing row (new key insert)."""

    async def _fake_db():
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        db = AsyncMock()
        db.execute = AsyncMock(return_value=result)
        yield db

    return _fake_db


def _fake_db_existing_key():
    """Fake async DB whose execute returns an existing DeviceKey row."""
    from app.models.device_key import DeviceKey

    existing = MagicMock(spec=DeviceKey)
    existing.public_key_cbor = b"old_key"
    existing.counter = 0

    async def _fake_db():
        result = MagicMock()
        result.scalar_one_or_none.return_value = existing
        db = AsyncMock()
        db.execute = AsyncMock(return_value=result)
        yield db

    return _fake_db


# ---------------------------------------------------------------------------
# POST /attestation/nonce
# ---------------------------------------------------------------------------


class TestNonce:
    def test_unauthenticated_returns_401(self):
        resp = TestClient(app).post("/attestation/nonce")
        assert resp.status_code == 401

    def test_unconfigured_secret_returns_503(self, monkeypatch):
        from app.auth.dependencies import get_current_user

        monkeypatch.setattr("app.api.attestation.settings.attestation_nonce_secret", "")
        app.dependency_overrides[get_current_user] = lambda: FAKE_USER
        try:
            resp = TestClient(app).post("/attestation/nonce")
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 503
        assert resp.json()["detail"] == "attestation_not_configured"

    def test_configured_secret_returns_200_with_dot_nonce(self, monkeypatch):
        from app.auth.dependencies import get_current_user

        monkeypatch.setattr(
            "app.api.attestation.settings.attestation_nonce_secret", _TEST_SECRET
        )
        app.dependency_overrides[get_current_user] = lambda: FAKE_USER
        try:
            resp = TestClient(app).post("/attestation/nonce")
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
        data = resp.json()
        assert "nonce" in data
        assert data["nonce"].count(".") == 1


# ---------------------------------------------------------------------------
# POST /attestation/register
# ---------------------------------------------------------------------------


class TestRegister:
    def test_unauthenticated_returns_401(self):
        resp = TestClient(app).post(
            "/attestation/register",
            json={"key_id": "abc", "attestation_b64": "ZmFrZQ==", "nonce": "x.y"},
        )
        assert resp.status_code == 401

    def test_unconfigured_app_id_returns_503(self, monkeypatch):
        from app.auth.dependencies import get_current_user
        from app.core.database import get_db

        monkeypatch.setattr("app.api.attestation.settings.attestation_apple_app_id", "")
        monkeypatch.setattr(
            "app.api.attestation.settings.attestation_nonce_secret", _TEST_SECRET
        )
        nonce = issue_nonce(FAKE_USER_ID, _TEST_SECRET)

        app.dependency_overrides[get_current_user] = lambda: FAKE_USER
        app.dependency_overrides[get_db] = _fake_db_new_key()
        try:
            resp = TestClient(app).post(
                "/attestation/register",
                json={
                    "key_id": "test-key-id",
                    "attestation_b64": "ZmFrZQ==",
                    "nonce": nonce,
                },
            )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 503
        assert resp.json()["detail"] == "attestation_not_configured"

    def test_invalid_nonce_returns_400(self, monkeypatch):
        from app.auth.dependencies import get_current_user
        from app.core.database import get_db

        monkeypatch.setattr(
            "app.api.attestation.settings.attestation_apple_app_id",
            "TEAM1234.com.example.app",
        )
        monkeypatch.setattr(
            "app.api.attestation.settings.attestation_nonce_secret", _TEST_SECRET
        )
        app.dependency_overrides[get_current_user] = lambda: FAKE_USER
        app.dependency_overrides[get_db] = _fake_db_new_key()
        try:
            resp = TestClient(app).post(
                "/attestation/register",
                json={
                    "key_id": "test-key-id",
                    "attestation_b64": "ZmFrZQ==",
                    "nonce": "invalid.nonce",
                },
            )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 400
        assert resp.json()["detail"] == "invalid_nonce"

    def test_apple_verifier_returns_none_gives_400(self, monkeypatch):
        """Stub verifier (default) returns None → 400 attestation_invalid."""
        from app.api.attestation import get_apple_verifier
        from app.auth.dependencies import get_current_user
        from app.core.database import get_db

        monkeypatch.setattr(
            "app.api.attestation.settings.attestation_apple_app_id",
            "TEAM1234.com.example.app",
        )
        monkeypatch.setattr(
            "app.api.attestation.settings.attestation_nonce_secret", _TEST_SECRET
        )
        nonce = issue_nonce(FAKE_USER_ID, _TEST_SECRET)

        # The default get_apple_verifier returns StubAppleAttestVerifier, which
        # returns None.  No override needed — just use the real dependency.
        app.dependency_overrides[get_current_user] = lambda: FAKE_USER
        app.dependency_overrides[get_db] = _fake_db_new_key()
        try:
            resp = TestClient(app).post(
                "/attestation/register",
                json={
                    "key_id": "test-key-id",
                    "attestation_b64": "ZmFrZQ==",
                    "nonce": nonce,
                },
            )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 400
        assert resp.json()["detail"] == "attestation_invalid"

    def test_successful_register_returns_200_and_commits(self, monkeypatch):
        """A verifier that returns a real AttestationResult → 200 and DB commit."""
        from app.api.attestation import get_apple_verifier
        from app.auth.dependencies import get_current_user
        from app.core.database import get_db

        monkeypatch.setattr(
            "app.api.attestation.settings.attestation_apple_app_id",
            "TEAM1234.com.example.app",
        )
        monkeypatch.setattr(
            "app.api.attestation.settings.attestation_nonce_secret", _TEST_SECRET
        )
        nonce = issue_nonce(FAKE_USER_ID, _TEST_SECRET)

        fake_result = AttestationResult(public_key_cbor=b"fake_key", counter=0)

        class _MockVerifier:
            async def verify_attestation(self, *_args, **_kwargs):
                return fake_result

        # Capture the db mock so we can assert commit was called.
        captured_db = []

        async def _capturing_db():
            result = MagicMock()
            result.scalar_one_or_none.return_value = None
            db = AsyncMock()
            db.execute = AsyncMock(return_value=result)
            captured_db.append(db)
            yield db

        app.dependency_overrides[get_current_user] = lambda: FAKE_USER
        app.dependency_overrides[get_db] = _capturing_db
        app.dependency_overrides[get_apple_verifier] = lambda: _MockVerifier()
        try:
            resp = TestClient(app).post(
                "/attestation/register",
                json={
                    "key_id": "test-key-id",
                    "attestation_b64": "ZmFrZQ==",
                    "nonce": nonce,
                },
            )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
        assert resp.json() == {"registered": True}
        # DB commit must have been called.
        assert captured_db, "DB fixture was not entered"
        captured_db[0].commit.assert_awaited_once()

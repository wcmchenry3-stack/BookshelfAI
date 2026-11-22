"""Unit tests for get_current_user dependency — exercises all error branches."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.auth.dependencies import get_current_user
from app.models.user import User

USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
FAKE_USER = User(id=USER_ID, email="test@example.com", token_version=0)


@pytest.fixture(scope="module")
def rsa_key_pair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private_key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


@pytest.fixture(autouse=True)
def patch_settings(rsa_key_pair):
    private_pem, public_pem = rsa_key_pair
    with patch("app.auth.jwt.settings") as mock:
        mock.jwt_private_key = private_pem
        mock.jwt_public_key = public_pem
        mock.jwt_access_ttl_minutes = 15
        mock.refresh_token_expiry_days = 7
        yield mock


async def _invoke(token: str, db_user=FAKE_USER):
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    db = AsyncMock()
    db.get = AsyncMock(return_value=db_user)
    return await get_current_user(credentials=credentials, db=db)


class TestGetCurrentUser:
    async def test_returns_user_for_valid_access_token(self):
        from app.auth.jwt import create_access_token

        token = create_access_token(str(USER_ID), "test@example.com", 0)
        user = await _invoke(token)
        assert user == FAKE_USER

    async def test_raises_401_for_expired_token(self, rsa_key_pair):
        private_pem, _ = rsa_key_pair
        payload = {
            "sub": str(USER_ID),
            "type": "access",
            "exp": datetime.now(UTC) - timedelta(seconds=1),
            "iat": datetime.now(UTC) - timedelta(hours=1),
        }
        expired = jwt.encode(payload, private_pem, algorithm="RS256")

        with pytest.raises(HTTPException) as exc_info:
            await _invoke(expired)
        assert exc_info.value.status_code == 401
        assert "expired" in exc_info.value.detail.lower()

    async def test_raises_401_for_invalid_token(self):
        with pytest.raises(HTTPException) as exc_info:
            await _invoke("not.a.valid.token")
        assert exc_info.value.status_code == 401
        assert "invalid" in exc_info.value.detail.lower()

    async def test_raises_401_for_refresh_token(self):
        from app.auth.jwt import create_refresh_token

        token, _ = create_refresh_token(str(USER_ID), 0)

        with pytest.raises(HTTPException) as exc_info:
            await _invoke(token)
        assert exc_info.value.status_code == 401
        assert "type" in exc_info.value.detail.lower()

    async def test_raises_401_when_user_not_in_db(self):
        from app.auth.jwt import create_access_token

        token = create_access_token(str(USER_ID), "test@example.com", 0)

        with pytest.raises(HTTPException) as exc_info:
            await _invoke(token, db_user=None)
        assert exc_info.value.status_code == 401
        assert "not found" in exc_info.value.detail.lower()

    async def test_raises_token_outdated_for_token_without_tv(self, rsa_key_pair):
        """#725: a pre-#689 access token (no tv) is a plain 401, not
        token_revoked, so the client refreshes instead of signing out."""
        from app.auth.dependencies import TOKEN_OUTDATED, TOKEN_REVOKED

        private_pem, _ = rsa_key_pair
        now = datetime.now(UTC)
        legacy = jwt.encode(
            {
                "sub": str(USER_ID),
                "email": "test@example.com",
                "type": "access",
                "iat": now,
                "exp": now + timedelta(minutes=15),
            },
            private_pem,
            algorithm="RS256",
        )

        with pytest.raises(HTTPException) as exc_info:
            await _invoke(legacy)
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == TOKEN_OUTDATED
        assert TOKEN_OUTDATED != TOKEN_REVOKED

"""Unit tests for JWT token creation and decoding (no DB required)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth.jwt import create_access_token, create_refresh_token, decode_token


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
        mock.jwt_expiry_hours = 24
        mock.refresh_token_expiry_days = 7
        yield mock


class TestCreateAccessToken:
    def test_contains_correct_claims(self, rsa_key_pair):
        _, public_pem = rsa_key_pair
        token = create_access_token("user-abc", "user@example.com")
        payload = jwt.decode(token, public_pem, algorithms=["RS256"])
        assert payload["sub"] == "user-abc"
        assert payload["email"] == "user@example.com"
        assert payload["type"] == "access"

    def test_uses_rs256_algorithm(self, rsa_key_pair):
        _, _public_pem = rsa_key_pair
        token = create_access_token("user-abc", "user@example.com")
        header = jwt.get_unverified_header(token)
        assert header["alg"] == "RS256"

    def test_expiry_matches_settings(self, rsa_key_pair, patch_settings):
        _, public_pem = rsa_key_pair
        patch_settings.jwt_expiry_hours = 2
        token = create_access_token("u", "e@x.com")
        payload = jwt.decode(token, public_pem, algorithms=["RS256"])
        now = datetime.now(timezone.utc)
        exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
        delta = exp - now
        assert 1.9 * 3600 < delta.total_seconds() < 2.1 * 3600


class TestCreateRefreshToken:
    def test_contains_correct_claims(self, rsa_key_pair):
        _, public_pem = rsa_key_pair
        token, jti = create_refresh_token("user-xyz")
        payload = jwt.decode(token, public_pem, algorithms=["RS256"])
        assert payload["sub"] == "user-xyz"
        assert payload["type"] == "refresh"
        assert payload["jti"] == jti

    def test_returns_unique_jti_each_call(self, rsa_key_pair):
        _, jti1 = create_refresh_token("u")
        _, jti2 = create_refresh_token("u")
        assert jti1 != jti2

    def test_refresh_has_no_email_claim(self, rsa_key_pair):
        _, public_pem = rsa_key_pair
        token, _ = create_refresh_token("user-xyz")
        payload = jwt.decode(token, public_pem, algorithms=["RS256"])
        assert "email" not in payload

    def test_expiry_matches_settings(self, rsa_key_pair, patch_settings):
        _, public_pem = rsa_key_pair
        patch_settings.refresh_token_expiry_days = 3
        token, _ = create_refresh_token("u")
        payload = jwt.decode(token, public_pem, algorithms=["RS256"])
        now = datetime.now(timezone.utc)
        exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
        delta = exp - now
        assert 2.9 * 86400 < delta.total_seconds() < 3.1 * 86400


class TestDecodeToken:
    def test_decodes_valid_access_token(self, rsa_key_pair):
        token = create_access_token("u1", "a@b.com")
        payload = decode_token(token)
        assert payload["sub"] == "u1"
        assert payload["type"] == "access"

    def test_decodes_valid_refresh_token(self):
        token, _ = create_refresh_token("u2")
        payload = decode_token(token)
        assert payload["sub"] == "u2"
        assert payload["type"] == "refresh"

    def test_raises_on_expired_token(self, rsa_key_pair):
        private_pem, _ = rsa_key_pair
        payload = {
            "sub": "u",
            "type": "access",
            "exp": datetime.now(timezone.utc) - timedelta(seconds=1),
            "iat": datetime.now(timezone.utc) - timedelta(hours=1),
        }
        expired_token = jwt.encode(payload, private_pem, algorithm="RS256")
        with pytest.raises(jwt.ExpiredSignatureError):
            decode_token(expired_token)

    def test_raises_on_tampered_token(self, rsa_key_pair):
        token = create_access_token("u", "e@x.com")
        tampered = token[:-5] + "XXXXX"
        with pytest.raises(jwt.InvalidTokenError):
            decode_token(tampered)

    def test_raises_on_garbage_input(self):
        with pytest.raises(jwt.InvalidTokenError):
            decode_token("not.a.token")


# ─── Security: algorithm attacks ──────────────────────────────────────────────


class TestAlgorithmSecurity:
    """Verify decode_token rejects tokens using unexpected algorithms."""

    def test_rejects_alg_none(self, rsa_key_pair):
        """'alg: none' unsigned tokens must never be accepted."""
        _private_pem, _ = rsa_key_pair
        payload = {
            "sub": "attacker",
            "type": "access",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "iat": datetime.now(timezone.utc),
        }
        # PyJWT refuses to encode with alg=none; craft the token manually.
        import base64
        import json

        header = base64.urlsafe_b64encode(
            json.dumps({"alg": "none", "typ": "JWT"}).encode()
        ).rstrip(b"=")
        body = base64.urlsafe_b64encode(
            json.dumps(
                {
                    **payload,
                    "exp": int(payload["exp"].timestamp()),
                    "iat": int(payload["iat"].timestamp()),
                }
            ).encode()
        ).rstrip(b"=")
        none_token = f"{header.decode()}.{body.decode()}."

        with pytest.raises(jwt.InvalidTokenError):
            decode_token(none_token)

    def test_rejects_hs256_token(self):
        """
        Algorithm confusion: decode_token must reject any HS256-signed token
        because it only accepts RS256. An attacker can't forge a valid RS256
        token, but they could try submitting an HS256 one.
        """
        payload = {
            "sub": "attacker",
            "type": "access",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "iat": datetime.now(timezone.utc),
        }
        hs256_token = jwt.encode(payload, "any-hmac-secret", algorithm="HS256")

        with pytest.raises(jwt.InvalidTokenError):
            decode_token(hs256_token)

    def test_rejects_token_signed_with_different_private_key(self):
        """Token signed with a different RSA key must not verify."""
        from cryptography.hazmat.primitives.asymmetric import rsa as _rsa

        other_key = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
        other_private_pem = other_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()

        payload = {
            "sub": "user",
            "type": "access",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "iat": datetime.now(timezone.utc),
        }
        foreign_token = jwt.encode(payload, other_private_pem, algorithm="RS256")

        with pytest.raises(jwt.InvalidTokenError):
            decode_token(foreign_token)

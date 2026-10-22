"""The tier is server-side only: forged client input never changes it."""

import io
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from PIL import Image

from app.ai import entitlements
from app.ai.entitlements import Tier
from app.ai.providers.base import ProviderResult
from app.auth.jwt import create_access_token
from app.main import app
from app.models.user import User

pytestmark = pytest.mark.security

USER = User(id=uuid.UUID("00000000-0000-0000-0000-000000000528"), email="f@example.com")


def _real_jpeg() -> bytes:
    # A real, decodable JPEG: since #529 an undecodable upload is rejected
    # with 415 image_undecodable instead of being passed through.
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), (200, 30, 30)).save(buf, format="JPEG")
    return buf.getvalue()


_JPEG = _real_jpeg()


@pytest.fixture(scope="module")
def rsa_pem():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private, public


@pytest.fixture(autouse=True)
def jwt_keys(rsa_pem):
    with patch("app.auth.jwt.settings") as mock:
        mock.jwt_private_key, mock.jwt_public_key = rsa_pem
        mock.jwt_expiry_hours = 1
        yield mock


def _current_private_key() -> str:
    from app.auth import jwt as auth_jwt

    return auth_jwt.settings.jwt_private_key


def _forged_token() -> str:
    """A validly signed access token that also carries tier claims."""
    now = datetime.now(UTC)
    payload = {
        "sub": str(USER.id),
        "email": USER.email,
        "iat": now,
        "exp": now + timedelta(hours=1),
        "type": "access",
        "tier": "premium",
        "plan": "premium",
    }
    return jwt.encode(payload, _current_private_key(), algorithm="RS256")


@pytest.fixture
def client():
    from app.core.database import get_db

    db = AsyncMock()
    db.get = AsyncMock(return_value=USER)

    async def _fake_db():
        yield db

    app.dependency_overrides[get_db] = _fake_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _scan(client, token, **kwargs):
    """POST /scan with the real auth dependency; return (response, tiers seen)."""
    seen = []
    real = entitlements.tier_for

    async def spy(db, user):
        tier = await real(db, user)
        seen.append(tier)
        return tier

    with (
        patch.object(entitlements, "tier_for", spy),
        patch("app.ai.gateway.OpenAIVisionProvider") as cls,
        patch("app.api.scan.EnrichmentService") as enrich,
        patch("app.api.scan.DeduplicationService") as dedup,
        patch("app.api.scan._reserve_scan_quota", new=AsyncMock()),
        patch("app.api.scan._release_scan_lease", new=AsyncMock()),
    ):
        cls.return_value.identify = AsyncMock(return_value=ProviderResult([]))
        enrich.return_value.enrich = AsyncMock(return_value=[])
        dedup.return_value.check = AsyncMock(return_value=[])
        headers = {"Authorization": f"Bearer {token}", **kwargs.pop("headers", {})}
        resp = client.post(
            "/scan",
            files={"file": ("s.jpg", io.BytesIO(_JPEG), "image/jpeg")},
            headers=headers,
            **kwargs,
        )
    return resp, seen


def test_baseline_is_free(client):
    resp, seen = _scan(client, create_access_token(str(USER.id), USER.email))
    assert resp.status_code == 200, resp.text
    assert seen == [Tier.FREE]


def test_forged_form_fields_ignored(client):
    resp, seen = _scan(
        client,
        create_access_token(str(USER.id), USER.email),
        data={"tier": "premium", "plan": "premium"},
    )
    assert resp.status_code == 200, resp.text
    assert seen == [Tier.FREE]


def test_forged_header_and_query_ignored(client):
    resp, seen = _scan(
        client,
        create_access_token(str(USER.id), USER.email),
        headers={"X-Tier": "premium", "X-Plan": "premium"},
        params={"tier": "premium", "plan": "premium"},
    )
    assert resp.status_code == 200, resp.text
    assert seen == [Tier.FREE]


def test_forged_jwt_claim_ignored(client):
    resp, seen = _scan(client, _forged_token())
    assert resp.status_code == 200, resp.text
    assert seen == [Tier.FREE]

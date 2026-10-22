"""Entitlement hook: Free by default, test-only override, client input ignored.

The Premium override is pytest's ``monkeypatch`` on the module function. There
is no flag or variable in production code that does the same.
"""

import inspect
import io
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.ai import entitlements, gateway
from app.ai.entitlements import Tier, tier_for
from app.ai.gateway import VisionPayload, run_feature
from app.main import app
from app.models.user import User

USER = User(id=uuid.UUID("00000000-0000-0000-0000-000000000527"), email="t@example.com")


def _real_jpeg() -> bytes:
    # A real, decodable JPEG: since #529 an undecodable upload is rejected
    # with 415 image_undecodable instead of being passed through.
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), (200, 30, 30)).save(buf, format="JPEG")
    return buf.getvalue()


_JPEG = _real_jpeg()


def test_tier_enum_members():
    assert {t.name for t in Tier} == {"FREE", "PREMIUM"}


async def test_default_is_free():
    assert await tier_for(MagicMock(), USER) is Tier.FREE


def test_signature_cannot_receive_a_request():
    params = list(inspect.signature(tier_for).parameters)
    assert params == ["db", "user"]


async def test_override_returns_premium(monkeypatch):
    async def premium(db, user):
        return Tier.PREMIUM

    monkeypatch.setattr(entitlements, "tier_for", premium)
    with patch("app.ai.gateway.OpenAIVisionProvider") as cls:
        cls.return_value.identify = AsyncMock(return_value=[])
        result = await run_feature(
            "scan.standard",
            user=USER,
            db=MagicMock(),
            payload=VisionPayload(image_bytes=b"i"),
        )
    assert result.tier is Tier.PREMIUM


async def test_gateway_result_is_free_by_default():
    with patch("app.ai.gateway.OpenAIVisionProvider") as cls:
        cls.return_value.identify = AsyncMock(return_value=[])
        result = await run_feature(
            "scan.standard",
            user=USER,
            db=MagicMock(),
            payload=VisionPayload(image_bytes=b"i"),
        )
    assert result.tier is Tier.FREE


@pytest.fixture
def client():
    from app.auth.dependencies import get_current_user
    from app.core.database import get_db

    async def _fake_db():
        yield AsyncMock()

    app.dependency_overrides[get_current_user] = lambda: USER
    app.dependency_overrides[get_db] = _fake_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_scan_ignores_forged_client_tier(client):
    seen = []
    real = gateway.run_feature

    async def spy(*args, **kwargs):
        result = await real(*args, **kwargs)
        seen.append(result.tier)
        return result

    with (
        patch("app.api.scan.run_feature", new=spy),
        patch("app.ai.gateway.OpenAIVisionProvider") as cls,
        patch("app.api.scan.EnrichmentService") as enrich,
        patch("app.api.scan.DeduplicationService") as dedup,
        patch("app.api.scan.scan_quota") as quota,
    ):
        cls.return_value.identify = AsyncMock(return_value=[])
        enrich.return_value.enrich = AsyncMock(return_value=[])
        dedup.return_value.check = AsyncMock(return_value=[])
        quota.global_scans_today = AsyncMock(return_value=0)
        quota.reserve_scan = AsyncMock(return_value=1)
        quota.refund_scan = AsyncMock()
        quota.utc_today = MagicMock()
        resp = client.post(
            "/scan?tier=premium",
            files={"file": ("s.jpg", io.BytesIO(_JPEG), "image/jpeg")},
            data={"tier": "premium", "plan": "premium"},
            headers={"X-Tier": "premium"},
        )

    assert resp.status_code == 200, resp.text
    assert seen == [Tier.FREE]

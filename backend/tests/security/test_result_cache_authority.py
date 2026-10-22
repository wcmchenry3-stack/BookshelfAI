"""Result-cache authority (#534): the cache is per user and every part of the
key is decided by the server. User B never gets user A's entry for the same
photo, a non-Premium user never gets an enhanced hit, nothing a client sends
(form fields, query parameters, headers) changes the key or what is stored,
and a cache row holds no image bytes and no raw model output.

Run on its own with:  pytest -m security tests/security/test_result_cache_authority.py
"""

import base64
import io
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import JSON, LargeBinary, String, Text, delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.datastructures import Headers
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.ai import cache, entitlements
from app.ai.entitlements import Tier
from app.ai.providers.base import ProviderResult
from app.ai.registry import FEATURES
from app.api import scan as scan_api
from app.core.config import settings
from app.core.database import Base
from app.main import app
from app.models.ai_result_cache import AIResultCache
from app.models.ai_usage import AIUsage
from app.models.user import User
from app.services.book_identifier import BookCandidate
from tests.unit._cache_fake import FakeResultCache
from tests.unit._quota_fake import FakeQuotaStore

pytestmark = pytest.mark.security

ALICE = User(id=uuid.UUID("00000000-0000-0000-0000-0000000534a1"), email="a@x.io")
BOB = User(id=uuid.UUID("00000000-0000-0000-0000-0000000534b2"), email="b@x.io")
DUNE = BookCandidate(title="Dune", author="Frank Herbert", confidence=0.9)


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (7, 5, 3)).save(buf, format="JPEG")
    return buf.getvalue()


_JPEG = _jpeg()


@pytest.fixture
def store():
    s = FakeQuotaStore()
    with s.installed():
        yield s


@pytest.fixture
def fake_cache():
    c = FakeResultCache()
    with c.installed():
        yield c


@pytest.fixture
def as_user():
    from app.auth.dependencies import get_current_user
    from app.core.database import get_db

    current = {"user": ALICE}

    async def _fake_db():
        yield AsyncMock()

    app.dependency_overrides[get_current_user] = lambda: current["user"]
    app.dependency_overrides[get_db] = _fake_db

    def switch(user: User) -> TestClient:
        current["user"] = user
        return TestClient(app)

    yield switch
    app.dependency_overrides.clear()


@pytest.fixture
def provider():
    with (
        patch("app.ai.gateway.OpenAIVisionProvider") as cls,
        patch("app.api.scan.EnrichmentService") as enrich,
        patch("app.api.scan.DeduplicationService") as dedup,
        patch("app.api.scan.ledger.record", AsyncMock()),
    ):
        cls.return_value.identify = AsyncMock(return_value=ProviderResult([DUNE]))
        enrich.return_value.enrich = AsyncMock(return_value=[])
        dedup.return_value.check = AsyncMock(return_value=[])
        yield cls


def _post(client, data=None, headers=None, params=None):
    return client.post(
        "/scan",
        files={"file": ("scan.jpg", io.BytesIO(_JPEG), "image/jpeg")},
        data=data or {},
        headers=headers or {},
        params=params or {},
    )


class TestPerUserScope:
    def test_user_b_cannot_hit_user_a_entry_with_the_same_photo(
        self, as_user, store, fake_cache, provider
    ):
        assert _post(as_user(ALICE)).status_code == 200
        assert _post(as_user(BOB)).status_code == 200
        assert provider.return_value.identify.await_count == 2
        a, b = fake_cache.stores
        assert a["image_hash"] == b["image_hash"]
        assert a["key"] != b["key"]
        assert (a["user_id"], b["user_id"]) == (ALICE.id, BOB.id)
        # Each now hits only their own entry.
        _post(as_user(ALICE))
        _post(as_user(BOB))
        assert provider.return_value.identify.await_count == 2
        assert fake_cache.lookups[-2:] == [a["key"], b["key"]]

    def test_forged_user_id_or_key_cannot_reach_another_users_entry(
        self, as_user, store, fake_cache, provider
    ):
        _post(as_user(ALICE))
        (alice,) = fake_cache.stores
        forged = {"user_id": str(ALICE.id), "cache_key": alice["key"]}
        resp = _post(
            as_user(BOB),
            data=forged,
            params=forged,
            headers={"X-User-Id": str(ALICE.id), "X-Cache-Key": alice["key"]},
        )
        assert resp.status_code == 200
        assert provider.return_value.identify.await_count == 2
        assert fake_cache.stores[-1]["user_id"] == BOB.id


class TestEntitlement:
    def test_non_premium_never_gets_an_enhanced_hit(
        self, as_user, store, fake_cache, provider, monkeypatch
    ):
        monkeypatch.setattr(
            entitlements, "tier_for", AsyncMock(return_value=Tier.PREMIUM)
        )
        assert _post(as_user(ALICE), data={"enhanced": "true"}).status_code == 200
        monkeypatch.setattr(entitlements, "tier_for", AsyncMock(return_value=Tier.FREE))
        forged = {"enhanced": "true", "tier": "premium", "is_premium": "true"}
        resp = _post(
            as_user(ALICE),
            data=forged,
            params={"tier": "premium"},
            headers={"X-Tier": "premium"},
        )
        assert (resp.status_code, resp.json()["detail"]) == (
            403,
            "enhanced_requires_premium",
        )
        assert provider.return_value.identify.await_count == 1


# Every cache field a client might try to set, as form fields, query
# parameters and headers.
_FORGED = {
    "cache_key": "f" * 64,
    "image_sha256": "e" * 64,
    "image_hash": "e" * 64,
    "user_id": str(BOB.id),
    "model": "gpt-4o",
    "prompt_version": "forged",
    "feature": "scan.enhanced",
    "cache": "false",
    "cache.enabled": "false",
    "ttl_days": "1",
    "max_rows_per_user": "1",
}


class TestForgedFields:
    def test_forged_fields_change_nothing(self, as_user, store, fake_cache, provider):
        assert _post(as_user(ALICE)).status_code == 200
        (honest,) = fake_cache.stores
        headers = {
            f"X-{k.replace('.', '-').replace('_', '-')}": v for k, v in _FORGED.items()
        }
        resp = _post(as_user(ALICE), data=_FORGED, params=_FORGED, headers=headers)
        assert resp.status_code == 200
        # The forged request looked up the honest key and was a hit.
        assert fake_cache.lookups == [honest["key"], honest["key"]]
        assert provider.return_value.identify.await_count == 1
        assert honest["user_id"] == ALICE.id
        assert honest["image_hash"] not in (_FORGED["image_sha256"],)
        assert honest["model"] == FEATURES["scan.standard"].model
        assert honest["feature"] == "scan.standard"
        assert honest["prompt_version"] != "forged"
        assert (honest["ttl_days"], honest["max_rows_per_user"]) == (30, 500)


# ---------------------------------------------------------------------------
# Column level
# ---------------------------------------------------------------------------


def test_columns_are_exactly_the_key_parts_and_the_result():
    assert {c.name for c in AIResultCache.__table__.columns} == {
        "cache_key",
        "user_id",
        "image_sha256",
        "feature",
        "model",
        "prompt_version",
        "result",
        "created_at",
        "expires_at",
    }


def test_no_column_can_hold_image_bytes_or_free_text():
    for column in AIResultCache.__table__.columns:
        assert not isinstance(column.type, LargeBinary | Text), column.name
        if isinstance(column.type, String):
            assert column.type.length is not None and column.type.length <= 64
    assert isinstance(AIResultCache.__table__.c.result.type, JSON)


def test_serialised_result_holds_only_candidate_fields():
    item = BookCandidate(
        title="T", author="A", confidence=0.5, isbn_13="9780000000002", isbn_10=None
    )
    assert cache.serialise([item]) == [
        {
            "title": "T",
            "author": "A",
            "confidence": 0.5,
            "isbn_13": "9780000000002",
            "isbn_10": None,
        }
    ]


@pytest.mark.integration
async def test_stored_row_has_no_image_bytes_and_no_raw_output():
    engine = create_async_engine(settings.async_database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    uid = uuid.uuid4()
    async with factory() as db:
        db.add(User(id=uid, email=f"{uid}@example.com", google_id=f"g-{uid}"))
        await db.commit()
    seen: list[bytes] = []

    async def identify(image):
        seen.append(image)
        return ProviderResult([DUNE])

    provider_cls = MagicMock()
    provider_cls.return_value.identify = identify
    try:
        with (
            patch("app.ai.gateway.OpenAIVisionProvider", provider_cls),
            patch("app.api.scan.EnrichmentService") as enrich,
            patch("app.api.scan.DeduplicationService") as dedup,
            patch.object(settings, "turnstile_required", False),
        ):
            enrich.return_value.enrich = AsyncMock(return_value=[])
            dedup.return_value.check = AsyncMock(return_value=[])
            async with factory() as db:
                user = await db.get(User, uid)
                await scan_api.scan.__wrapped__(
                    request=MagicMock(),
                    file=StarletteUploadFile(
                        file=io.BytesIO(_JPEG),
                        filename="scan.jpg",
                        headers=Headers({"content-type": "image/jpeg"}),
                    ),
                    current_user=user,
                    db=db,
                    cf_turnstile_response=None,
                    enhanced=False,
                )
        async with factory() as db:
            (row,) = (
                await db.execute(
                    select(AIResultCache).where(AIResultCache.user_id == uid)
                )
            ).scalars()
        (sent,) = seen
        assert row.image_sha256 == cache.image_sha256(sent)
        assert row.result == [
            {
                "title": "Dune",
                "author": "Frank Herbert",
                "confidence": 0.9,
                "isbn_13": None,
                "isbn_10": None,
            }
        ]
        values = " ".join(
            str(getattr(row, c.name)) for c in AIResultCache.__table__.columns
        )
        for image in (sent, _JPEG):
            assert base64.b64encode(image).decode()[:40] not in values
            assert image.hex()[:80] not in values
    finally:
        async with factory() as db:
            await db.execute(delete(AIUsage).where(AIUsage.user_id == uid))
            await db.execute(delete(User).where(User.id == uid))
            await db.commit()
        await engine.dispose()

"""Scan-record authority (#585): records are per user and keyed by the server.
User B never gets user A's record for the same photo; a client cannot name a
fingerprint, a user, a tier, a model or a step (form fields, query
parameters, headers change nothing); a non-Premium user never gets an
enhanced repeat; and a stored record holds no image bytes and no raw model
output.

Run on its own with:  pytest -m security tests/security/test_scan_records_authority.py
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

from app.ai import entitlements, scan_records
from app.ai.entitlements import Tier
from app.ai.providers.base import ProviderResult
from app.ai.registry import FEATURES
from app.api import scan as scan_api
from app.core.config import settings
from app.core.database import Base
from app.main import app
from app.models.ai_usage import AIUsage
from app.models.scan_record import ScanRecord
from app.models.user import User
from app.schemas.book import EnrichedBook
from app.services.book_identifier import BookCandidate
from tests.unit._quota_fake import FakeQuotaStore
from tests.unit._scan_records_fake import FakeScanRecords

pytestmark = pytest.mark.security

ALICE = User(id=uuid.UUID("00000000-0000-0000-0000-0000000585a1"), email="a@x.io")
BOB = User(id=uuid.UUID("00000000-0000-0000-0000-0000000585b2"), email="b@x.io")
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
def records():
    r = FakeScanRecords()
    with r.installed():
        yield r


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

        async def _enrich(candidates, limit):
            enrich.return_value.last_complete = True
            return [
                EnrichedBook(title=c.title, author=c.author, confidence=c.confidence)
                for c in candidates
            ]

        cls.return_value.identify = AsyncMock(return_value=ProviderResult([DUNE]))
        enrich.return_value.enrich = AsyncMock(side_effect=_enrich)
        dedup.return_value.check = AsyncMock(side_effect=lambda _d, _u, b: b)
        yield cls


def _post(client, data=None, headers=None, params=None, image=_JPEG):
    return client.post(
        "/scan",
        files={"file": ("scan.jpg", io.BytesIO(image), "image/jpeg")},
        data=data or {},
        headers=headers or {},
        params=params or {},
    )


class TestPerUserScope:
    def test_user_b_never_gets_user_a_record_for_the_same_photo(
        self, as_user, store, records, provider
    ):
        assert _post(as_user(ALICE)).json()["repeat"] is False
        assert _post(as_user(BOB)).json()["repeat"] is False
        assert provider.return_value.identify.await_count == 2
        alice_fp, bob_fp = [fp for (_, fp) in records.rows]
        assert alice_fp == bob_fp  # same photo, separate records
        assert {u for (u, _) in records.rows} == {ALICE.id, BOB.id}
        # Each now repeats only their own record.
        assert _post(as_user(ALICE)).json()["repeat"] is True
        assert _post(as_user(BOB)).json()["repeat"] is True
        assert provider.return_value.identify.await_count == 2

    def test_forged_user_or_fingerprint_cannot_reach_another_users_record(
        self, as_user, store, records, provider
    ):
        _post(as_user(ALICE))
        ((_, alice_fp),) = records.rows
        forged = {"user_id": str(ALICE.id), "fingerprint": alice_fp}
        resp = _post(
            as_user(BOB),
            data=forged,
            params=forged,
            headers={"X-User-Id": str(ALICE.id), "X-Fingerprint": alice_fp},
        )
        assert resp.status_code == 200
        assert resp.json()["repeat"] is False
        assert provider.return_value.identify.await_count == 2
        assert (BOB.id, alice_fp) in records.rows

    def test_client_cannot_name_a_fingerprint_for_another_photo(
        self, as_user, store, records, provider
    ):
        """A forged fingerprint of a photo Alice scanned, sent with a
        different photo, is ignored: the fingerprint comes from the bytes."""
        _post(as_user(ALICE))
        ((_, fp),) = records.rows
        other = io.BytesIO()
        Image.new("RGB", (16, 16), (200, 1, 1)).save(other, format="JPEG")
        resp = _post(
            as_user(ALICE),
            data={"fingerprint": fp, "repeat": "true"},
            params={"fingerprint": fp},
            headers={"X-Fingerprint": fp},
            image=other.getvalue(),
        )
        assert resp.json()["repeat"] is False
        assert provider.return_value.identify.await_count == 2
        assert len(records.rows) == 2


class TestEntitlement:
    def test_non_premium_never_gets_an_enhanced_repeat(
        self, as_user, store, records, provider, monkeypatch
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


# Every record field a client might try to set, as form fields, query
# parameters and headers.
_FORGED = {
    "fingerprint": "f" * 64,
    "user_id": str(BOB.id),
    "tier": "premium",
    "model": "gpt-4o",
    "feature": "scan.enhanced",
    "ai_done": "true",
    "enrich_done": "true",
    "repeat": "true",
    "scan_records.enabled": "false",
    "retention_days": "1",
    "max_per_user": "1",
}


class TestForgedFields:
    def test_forged_fields_change_nothing(self, as_user, store, records, provider):
        assert _post(as_user(ALICE)).status_code == 200
        (((user_id, fp), honest),) = records.rows.items()
        headers = {
            f"X-{k.replace('.', '-').replace('_', '-')}": v for k, v in _FORGED.items()
        }
        resp = _post(as_user(ALICE), data=_FORGED, params=_FORGED, headers=headers)
        assert resp.status_code == 200
        # The forged request found the honest record and was a repeat.
        assert resp.json()["repeat"] is True
        assert provider.return_value.identify.await_count == 1
        assert list(records.rows) == [(ALICE.id, fp)]
        assert user_id == ALICE.id and fp != _FORGED["fingerprint"]
        assert honest["model"] == FEATURES["scan.standard"].model
        assert (honest["feature"], honest["tier"]) == ("scan.standard", "free")
        assert honest["max_per_user"] == 500


# ---------------------------------------------------------------------------
# Column level
# ---------------------------------------------------------------------------


def test_columns_are_exactly_the_record_fields():
    assert {c.name for c in ScanRecord.__table__.columns} == {
        "id",
        "user_id",
        "fingerprint",
        "feature",
        "tier",
        "model",
        "candidates",
        "enrichment",
        "ai_done",
        "enrich_done",
        "created_at",
        "updated_at",
    }


def test_no_column_can_hold_image_bytes_or_free_text():
    for column in ScanRecord.__table__.columns:
        assert not isinstance(column.type, LargeBinary | Text), column.name
        if isinstance(column.type, String):
            assert column.type.length is not None and column.type.length <= 64
    for name in ("candidates", "enrichment"):
        assert isinstance(ScanRecord.__table__.c[name].type, JSON)


def test_serialised_candidates_hold_only_candidate_fields():
    item = BookCandidate(
        title="T", author="A", confidence=0.5, isbn_13="9780000000002", isbn_10=None
    )
    assert scan_records.serialise_candidates([item]) == [
        {
            "title": "T",
            "author": "A",
            "confidence": 0.5,
            "isbn_13": "9780000000002",
            "isbn_10": None,
        }
    ]


@pytest.mark.integration
async def test_stored_record_has_no_image_bytes_and_no_raw_output():
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
            patch.object(settings, "turnstile_required", False),
        ):

            async def _enrich(candidates, limit):
                enrich.return_value.last_complete = True
                return [
                    EnrichedBook(title=c.title, author=c.author, confidence=0.9)
                    for c in candidates
                ]

            enrich.return_value.enrich = AsyncMock(side_effect=_enrich)
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
                await db.execute(select(ScanRecord).where(ScanRecord.user_id == uid))
            ).scalars()
        (sent,) = seen
        assert row.fingerprint == scan_records.fingerprint(sent)
        assert row.candidates == [
            {
                "title": "Dune",
                "author": "Frank Herbert",
                "confidence": 0.9,
                "isbn_13": None,
                "isbn_10": None,
            }
        ]
        values = " ".join(
            str(getattr(row, c.name)) for c in ScanRecord.__table__.columns
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

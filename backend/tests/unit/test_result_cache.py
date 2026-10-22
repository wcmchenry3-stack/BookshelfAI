"""Unit tests for the per-user scan result cache (#534): the key, expiry and
serialisation in app/ai/cache.py, the settings, the prompt versions exposed
by the gateway, the cleanup wiring, and the /scan wiring (what is stored, what
a hit skips, and that a cache failure never fails a scan). Real Postgres
behaviour is in tests/integration/test_result_cache_db.py."""

import asyncio
import io
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.ai import cache, entitlements, gateway
from app.ai import settings as ai_settings
from app.ai.entitlements import Tier
from app.ai.providers import anthropic_vision, openai_vision
from app.ai.providers.base import ProviderResult, ProviderTimeoutError
from app.ai.registry import FEATURES
from app.main import app
from app.models.user import User
from app.schemas.book import EnrichedBook
from app.services.book_identifier import BookCandidate, ScanUnavailableError
from tests.unit._cache_fake import FakeResultCache
from tests.unit._quota_fake import FakeQuotaStore

USER = User(
    id=uuid.UUID("00000000-0000-0000-0000-00000000534a"),
    email="cache@example.com",
)
DUNE = BookCandidate(
    title="Dune",
    author="Frank Herbert",
    confidence=0.9,
    isbn_13="9780441013593",
    isbn_10=None,
)
KEY_PARTS = {
    "user_id": USER.id,
    "image_hash": "a" * 64,
    "feature": "scan.standard",
    "model": "gpt-4o-mini",
    "prompt_version": "2026-10-1",
}


def _jpeg(color=(10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(buf, format="JPEG")
    return buf.getvalue()


_JPEG = _jpeg()


# ---------------------------------------------------------------------------
# Key, expiry, serialisation
# ---------------------------------------------------------------------------


class TestKey:
    def test_is_sha256_hex_and_deterministic(self):
        key = cache.cache_key(**KEY_PARTS)
        assert len(key) == 64 and int(key, 16) >= 0
        assert key == cache.cache_key(**KEY_PARTS)

    @pytest.mark.parametrize(
        "part,other",
        [
            ("user_id", uuid.UUID("00000000-0000-0000-0000-00000000534b")),
            ("image_hash", "b" * 64),
            ("feature", "scan.enhanced"),
            ("model", "gpt-4o"),
            ("prompt_version", "2026-10-2"),
        ],
    )
    def test_changes_with_every_part(self, part, other):
        assert cache.cache_key(**KEY_PARTS) != cache.cache_key(
            **{**KEY_PARTS, part: other}
        )

    def test_parts_cannot_be_shifted_across_the_separator(self):
        a = cache.cache_key(**{**KEY_PARTS, "feature": "ab", "model": "c"})
        b = cache.cache_key(**{**KEY_PARTS, "feature": "a", "model": "bc"})
        assert a != b

    def test_separator_in_a_part_is_rejected(self):
        with pytest.raises(ValueError):
            cache.cache_key(**{**KEY_PARTS, "model": "x\x1fy"})

    def test_image_hash_is_sha256_of_the_bytes(self):
        import hashlib

        assert cache.image_sha256(b"abc") == hashlib.sha256(b"abc").hexdigest()


def test_expires_at_is_ttl_days_after_created_at():
    created = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
    assert cache.expires_at(created, 30) == created + timedelta(days=30)


class TestSerialisation:
    def test_only_candidate_fields(self):
        (item,) = cache.serialise([DUNE])
        assert set(item) == {"title", "author", "confidence", "isbn_13", "isbn_10"}
        assert cache.deserialise([item]) == [DUNE]

    def test_empty_list_round_trips(self):
        assert cache.deserialise(cache.serialise([])) == []

    @pytest.mark.parametrize(
        "stored",
        [
            None,
            {"books": []},
            "[]",
            [{"title": "Dune", "author": "F", "confidence": 0.5, "raw": "x"}],
            [{"title": "Dune", "author": "F", "confidence": 2.0}],
            [{"title": "Dune", "author": "F", "confidence": True}],
            [{"title": "Dune", "author": "F", "confidence": float("nan")}],
            [{"title": 1, "author": "F", "confidence": 0.5}],
            [{"title": "x" * 501, "author": "F", "confidence": 0.5}],
            [{"title": "Dune", "author": "F", "confidence": 0.5, "isbn_13": 9}],
            ["Dune"],
        ],
    )
    def test_invalid_rows_are_rejected(self, stored):
        with pytest.raises(cache.CacheEntryError):
            cache.deserialise(stored)

    async def test_lookup_treats_an_invalid_row_as_a_miss(self):
        db = AsyncMock()
        result = MagicMock()
        result.first.return_value = ({"not": "a list"},)
        db.execute = AsyncMock(return_value=result)
        assert await cache.lookup(db, key="k" * 64) is None
        db.commit.assert_awaited()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class TestSettings:
    def test_defaults(self):
        ai = ai_settings.AISettings({})
        assert ai.cache_enabled is True
        assert ai.cache_max_rows_per_user == 500
        assert ai.cache_ttl_days == 30

    @pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
    def test_invalid_enabled_fails_closed(self, value):
        ai_settings.reset_cache({"cache.enabled": value})
        rows = ai_settings._cache.rows
        assert ai_settings.AISettings(rows).cache_enabled is False

    @pytest.mark.parametrize(
        "key,value",
        [
            ("cache.max_rows_per_user", 0),
            ("cache.max_rows_per_user", 10_001),
            ("cache.ttl_days", 0),
            ("cache.ttl_days", 31),
        ],
    )
    def test_out_of_bounds_ignored_on_read(self, key, value):
        ai_settings.reset_cache({key: value})
        ai = ai_settings.AISettings(ai_settings._cache.rows)
        assert ai.get(key) == ai_settings.DEFINITIONS[key].default()


# ---------------------------------------------------------------------------
# Prompt versions (exposed by the gateway, defined next to each prompt)
# ---------------------------------------------------------------------------


class TestPromptVersion:
    def test_each_adapter_has_its_own(self):
        openai_spec = FEATURES["scan.standard"]
        assert gateway.prompt_version(openai_spec) == openai_vision.PROMPT_VERSION
        claude_spec = ai_settings.AISettings(
            {
                "scan.standard.provider": "anthropic",
                "scan.standard.model": "claude-haiku-5-5",
            }
        ).spec_for(openai_spec)
        assert gateway.prompt_version(claude_spec) == anthropic_vision.PROMPT_VERSION

    def test_unknown_provider_raises(self):
        spec = MagicMock(provider="nope")
        with pytest.raises(ValueError):
            gateway.prompt_version(spec)


# ---------------------------------------------------------------------------
# Daily cleanup
# ---------------------------------------------------------------------------


def _cleanup_db():
    result = MagicMock(rowcount=0)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=session), session


async def _one_cleanup_run(factory, purge):
    from app.services.token_cleanup import cleanup_expired_tokens

    with (
        patch("app.services.token_cleanup.AsyncSessionLocal", factory),
        patch("app.services.token_cleanup.purge_old_scan_usage", AsyncMock()),
        patch("app.services.token_cleanup.purge_old_quota", AsyncMock()),
        patch(
            "app.services.token_cleanup.rollup_and_purge",
            AsyncMock(return_value=(0, 0)),
        ),
        patch("app.services.token_cleanup.purge_old_spend", AsyncMock()),
        patch("app.services.token_cleanup.purge_old_zero_results", AsyncMock()),
        patch("app.services.token_cleanup.purge_expired_results", purge),
        patch("app.services.token_cleanup._INITIAL_DELAY_SECONDS", 0),
        patch("app.services.token_cleanup._INTERVAL_SECONDS", 9999),
        pytest.raises(asyncio.CancelledError),
    ):
        task = asyncio.create_task(cleanup_expired_tokens())
        for _ in range(10):
            await asyncio.sleep(0)
        task.cancel()
        await task


class TestCleanup:
    async def test_run_purges_expired_results(self):
        factory, _ = _cleanup_db()
        purge = AsyncMock(return_value=2)
        await _one_cleanup_run(factory, purge)
        purge.assert_awaited_once()

    async def test_purge_failure_does_not_skip_token_cleanup(self, caplog):
        factory, session = _cleanup_db()
        await _one_cleanup_run(factory, AsyncMock(side_effect=ConnectionError("x")))
        assert "result cache" in caplog.text
        assert "refresh_tokens" in str(session.execute.call_args[0][0])


# ---------------------------------------------------------------------------
# /scan wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def store():
    s = FakeQuotaStore(default_user=USER.id)
    with s.installed():
        yield s


@pytest.fixture
def fake_cache():
    c = FakeResultCache()
    with c.installed():
        yield c


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


@pytest.fixture
def ledger_rows():
    rows: list[dict] = []

    async def record(_db, **fields):
        rows.append(fields)

    with patch("app.api.scan.ledger.record", record):
        yield rows


@pytest.fixture
def provider():
    with (
        patch("app.ai.gateway.OpenAIVisionProvider") as cls,
        patch("app.api.scan.EnrichmentService") as enrich,
        patch("app.api.scan.DeduplicationService") as dedup,
    ):

        async def _enrich(candidates, limit):
            return [
                EnrichedBook(title=c.title, author=c.author, confidence=c.confidence)
                for c in candidates
            ]

        cls.return_value.identify = AsyncMock(return_value=ProviderResult([DUNE]))
        enrich.return_value.enrich = AsyncMock(side_effect=_enrich)
        dedup.return_value.check = AsyncMock(side_effect=lambda _d, _u, b: b)
        yield cls


def _post(client, image=_JPEG, enhanced=False):
    data = {"enhanced": "true"} if enhanced else {}
    return client.post(
        "/scan",
        files={"file": ("scan.jpg", io.BytesIO(image), "image/jpeg")},
        data=data,
    )


def _snapshot(store: FakeQuotaStore):
    return (
        dict(store.rows),
        dict(store.leases),
        {d: list(v) for d, v in store.spend.items()},
        {u: list(v) for u, v in store.zero_results.items()},
    )


class TestScanStoresOnlyOk:
    def test_ok_result_is_stored_and_served(
        self, client, store, fake_cache, provider, ledger_rows
    ):
        assert _post(client).status_code == 200
        assert len(fake_cache.stores) == 1
        stored = fake_cache.stores[0]
        assert stored["user_id"] == USER.id
        assert stored["feature"] == "scan.standard"
        assert stored["model"] == FEATURES["scan.standard"].model
        assert stored["prompt_version"] == openai_vision.PROMPT_VERSION
        assert stored["ttl_days"] == 30 and stored["max_rows_per_user"] == 500
        assert stored["image_hash"] != cache.image_sha256(_JPEG)  # normalised

    def test_empty_ok_result_is_stored(self, client, store, fake_cache, provider):
        provider.return_value.identify = AsyncMock(return_value=ProviderResult([]))
        assert _post(client).status_code == 200
        assert len(fake_cache.stores) == 1
        assert fake_cache.stores[0]["candidates"] == []

    @pytest.mark.parametrize("outcome", ["refused", "truncated"])
    def test_refused_or_truncated_not_stored(
        self, client, store, fake_cache, provider, outcome
    ):
        provider.return_value.identify = AsyncMock(
            return_value=ProviderResult([DUNE], outcome=outcome)
        )
        assert _post(client).status_code == 200
        assert fake_cache.stores == []
        assert fake_cache.rows == {}

    @pytest.mark.parametrize(
        "error",
        [
            ScanUnavailableError("down"),
            ProviderTimeoutError("slow"),
            RuntimeError("boom"),
        ],
    )
    def test_errors_not_stored(self, store, fake_cache, provider, error):
        from app.auth.dependencies import get_current_user
        from app.core.database import get_db

        async def _fake_db():
            yield AsyncMock()

        app.dependency_overrides[get_current_user] = lambda: USER
        app.dependency_overrides[get_db] = _fake_db
        try:
            provider.return_value.identify = AsyncMock(side_effect=error)
            resp = _post(TestClient(app, raise_server_exceptions=False))
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code in (500, 503)
        assert fake_cache.stores == []

    def test_cache_disabled_neither_looks_up_nor_stores(
        self, client, store, fake_cache, provider
    ):
        ai_settings.reset_cache({"cache.enabled": False})
        assert _post(client).status_code == 200
        assert _post(client).status_code == 200
        assert fake_cache.lookups == [] and fake_cache.stores == []
        assert provider.return_value.identify.await_count == 2


class TestScanHit:
    def test_hit_makes_no_provider_call_and_takes_nothing(
        self, client, store, fake_cache, provider, ledger_rows
    ):
        first = _post(client)
        assert first.status_code == 200
        before = _snapshot(store)
        releases = store.releases

        second = _post(client)
        assert second.status_code == 200
        assert second.json() == first.json()
        assert provider.return_value.identify.await_count == 1
        # No reservation, lease, count-cap unit, cost estimate or zero result.
        assert _snapshot(store) == before
        assert store.releases == releases
        (row,) = ledger_rows
        assert row["status"] == "cache_hit"
        assert row["user_id"] == USER.id
        assert row["feature"] == "scan.standard"
        assert row["model"] == FEATURES["scan.standard"].model
        assert isinstance(row["call_id"], uuid.UUID)
        assert "input_tokens" not in row  # 0 tokens, cost forced to 0

    def test_hit_still_returns_when_the_ledger_row_fails(
        self, client, store, fake_cache, provider
    ):
        _post(client)
        with patch(
            "app.api.scan.ledger.record", AsyncMock(side_effect=RuntimeError("x"))
        ):
            resp = _post(client)
        assert resp.status_code == 200
        assert provider.return_value.identify.await_count == 1

    def test_enhanced_hit_premium_reports_remaining_unchanged(
        self, client, store, fake_cache, provider, ledger_rows, monkeypatch
    ):
        monkeypatch.setattr(
            entitlements, "tier_for", AsyncMock(return_value=Tier.PREMIUM)
        )
        provider.return_value.identify = AsyncMock(return_value=ProviderResult([]))
        first = _post(client, enhanced=True)
        assert first.status_code == 200
        zero_before = {u: list(v) for u, v in store.zero_results.items()}
        second = _post(client, enhanced=True)
        assert second.status_code == 200
        assert provider.return_value.identify.await_count == 1
        assert second.json()["enhanced_scans_remaining"] == (
            first.json()["enhanced_scans_remaining"]
        )
        assert store.zero_results == zero_before

    def test_enhanced_hit_refused_for_non_premium(
        self, client, store, fake_cache, provider, ledger_rows, monkeypatch
    ):
        monkeypatch.setattr(
            entitlements, "tier_for", AsyncMock(return_value=Tier.PREMIUM)
        )
        assert _post(client, enhanced=True).status_code == 200
        # The user leaves Premium; the entry is still there.
        monkeypatch.setattr(entitlements, "tier_for", AsyncMock(return_value=Tier.FREE))
        before = _snapshot(store)
        resp = _post(client, enhanced=True)
        assert (resp.status_code, resp.json()["detail"]) == (
            403,
            "enhanced_requires_premium",
        )
        assert ledger_rows == []
        assert provider.return_value.identify.await_count == 1
        assert _snapshot(store) == before

    def test_standard_and_enhanced_do_not_share_an_entry(
        self, client, store, fake_cache, provider, monkeypatch
    ):
        monkeypatch.setattr(
            entitlements, "tier_for", AsyncMock(return_value=Tier.PREMIUM)
        )
        _post(client)
        _post(client, enhanced=True)
        assert provider.return_value.identify.await_count == 2

    def test_different_photo_misses(self, client, store, fake_cache, provider):
        _post(client)
        _post(client, image=_jpeg((200, 10, 10)))
        assert provider.return_value.identify.await_count == 2

    def test_expired_entry_is_not_served(self, client, store, fake_cache, provider):
        _post(client)
        (key,) = fake_cache.rows
        fake_cache.rows[key]["expires_at"] = cache.utc_now() - timedelta(seconds=1)
        _post(client)
        assert provider.return_value.identify.await_count == 2


class TestCacheFailureNeverFailsAScan:
    def test_lookup_raises_falls_through_to_the_provider(self, client, store, provider):
        with (
            patch(
                "app.api.scan.cache.lookup",
                AsyncMock(side_effect=RuntimeError("db blip")),
            ),
            patch("app.api.scan.cache.store", AsyncMock()),
        ):
            resp = _post(client)
        assert resp.status_code == 200
        assert resp.json()["books"][0]["title"] == "Dune"
        provider.return_value.identify.assert_awaited_once()
        assert any(store.rows.values())  # the normal path reserved

    def test_store_raises_still_returns_the_result(self, client, store, provider):
        with (
            patch("app.api.scan.cache.lookup", AsyncMock(return_value=None)),
            patch(
                "app.api.scan.cache.store",
                AsyncMock(side_effect=RuntimeError("db blip")),
            ),
        ):
            resp = _post(client)
        assert resp.status_code == 200
        assert resp.json()["books"][0]["title"] == "Dune"

    def test_failure_logs_carry_no_content(self, client, store, provider, caplog):
        with (
            patch(
                "app.api.scan.cache.lookup",
                AsyncMock(side_effect=RuntimeError("SECRET-TITLE")),
            ),
            patch(
                "app.api.scan.cache.store",
                AsyncMock(side_effect=RuntimeError("SECRET-TITLE")),
            ),
        ):
            assert _post(client).status_code == 200
        assert "result cache lookup failed" in caplog.text
        assert "result cache store failed" in caplog.text
        assert "SECRET-TITLE" not in caplog.text

    def test_key_failure_is_a_miss(self, client, store, fake_cache, provider):
        with patch(
            "app.api.scan.cache.cache_key", MagicMock(side_effect=ValueError("x"))
        ):
            assert _post(client).status_code == 200
        assert fake_cache.lookups == [] and fake_cache.stores == []

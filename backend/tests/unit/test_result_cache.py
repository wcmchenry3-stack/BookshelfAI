"""Unit tests for the per-user scan result cache (#534): the key, expiry and
serialisation in app/ai/cache.py, the settings, the prompt versions exposed
by the gateway, the cleanup wiring, and the /scan wiring (what is stored, what
a hit skips, and that a cache failure never fails a scan). Real Postgres
behaviour is in tests/integration/test_result_cache_db.py."""

import asyncio
import io
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.ai import cache, entitlements, gateway, quota
from app.ai import settings as ai_settings
from app.ai.entitlements import Tier
from app.ai.providers import anthropic_vision, openai_vision
from app.ai.providers.base import ProviderResult, ProviderTimeoutError
from app.ai.registry import FEATURES
from app.core.config import settings
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

    def test_candidate_cap_is_scan_max_books(self, monkeypatch):
        monkeypatch.setattr(settings, "scan_max_books", 2)
        assert len(cache.serialise([DUNE, DUNE])) == 2
        with pytest.raises(cache.CacheEntryError):
            cache.serialise([DUNE] * 3)
        with pytest.raises(cache.CacheEntryError):
            cache.deserialise(cache.serialise([DUNE]) * 3)

    async def test_lookup_treats_an_invalid_row_as_a_miss(self):
        db = AsyncMock()
        result = MagicMock()
        result.first.return_value = ({"not": "a list"},)
        db.execute = AsyncMock(return_value=result)
        assert await cache.lookup(db, key="k" * 64, user_id=USER.id) is None
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


_CLAUDE = {
    "scan.standard.provider": "anthropic",
    "scan.standard.model": "claude-haiku-5-5",
}


def _spec(rows=None, **changes):
    spec = ai_settings.AISettings(rows or {}).spec_for(FEATURES["scan.standard"])
    return replace(spec, **changes) if changes else spec


class TestRequestFingerprint:
    """The key's prompt_version part is the gateway's fingerprint of the
    request shape: any change to what is sent misses the cache."""

    @pytest.mark.parametrize("rows", [{}, _CLAUDE])
    def test_same_inputs_same_fingerprint(self, rows):
        fp = gateway.request_fingerprint(_spec(rows))
        assert len(fp) == 64 and int(fp, 16) >= 0
        assert gateway.request_fingerprint(_spec(rows)) == fp

    @pytest.mark.parametrize("rows", [{}, _CLAUDE])
    def test_max_books_changes_it(self, rows, monkeypatch):
        before = gateway.request_fingerprint(_spec(rows))
        monkeypatch.setattr(settings, "scan_max_books", settings.scan_max_books + 1)
        assert gateway.request_fingerprint(_spec(rows)) != before

    @pytest.mark.parametrize("rows", [{}, _CLAUDE])
    @pytest.mark.parametrize("change", [{"effort": "high"}, {"max_tokens": 1024}])
    def test_effort_and_max_tokens_change_it(self, rows, change):
        assert gateway.request_fingerprint(
            _spec(rows, **change)
        ) != gateway.request_fingerprint(_spec(rows))

    @pytest.mark.parametrize(
        "rows,module", [({}, openai_vision), (_CLAUDE, anthropic_vision)]
    )
    def test_system_prompt_text_changes_it(self, rows, module, monkeypatch):
        before = gateway.request_fingerprint(_spec(rows))
        monkeypatch.setattr(module, "SYSTEM_PROMPT", module.SYSTEM_PROMPT + " ")
        assert gateway.request_fingerprint(_spec(rows)) != before

    def test_claude_user_text_and_schema_change_it(self, monkeypatch):
        before = gateway.request_fingerprint(_spec(_CLAUDE))
        monkeypatch.setattr(anthropic_vision, "USER_TEXT", "Different.")
        after_text = gateway.request_fingerprint(_spec(_CLAUDE))
        assert after_text != before
        monkeypatch.setattr(
            anthropic_vision,
            "output_schema",
            lambda n: {"type": "object", "properties": {}, "required": []},
        )
        assert gateway.request_fingerprint(_spec(_CLAUDE)) != after_text

    @pytest.mark.parametrize(
        "rows,module", [({}, openai_vision), (_CLAUDE, anthropic_vision)]
    )
    def test_prompt_version_constant_changes_it(self, rows, module, monkeypatch):
        before = gateway.request_fingerprint(_spec(rows))
        monkeypatch.setattr(module, "PROMPT_VERSION", "2099-01-1")
        assert gateway.request_fingerprint(_spec(rows)) != before

    def test_provider_changes_it(self):
        assert gateway.request_fingerprint(_spec()) != gateway.request_fingerprint(
            _spec(_CLAUDE)
        )

    def test_unknown_provider_raises(self):
        spec = MagicMock(provider="nope")
        with pytest.raises(ValueError):
            gateway.request_fingerprint(spec)

    def test_key_changes_with_max_books(self, monkeypatch):
        def key():
            return cache.cache_key(
                **{
                    **KEY_PARTS,
                    "prompt_version": gateway.request_fingerprint(_spec()),
                }
            )

        before = key()
        monkeypatch.setattr(settings, "scan_max_books", settings.scan_max_books + 1)
        assert key() != before


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


def _hits(store: FakeQuotaStore, user_id=USER.id) -> int:
    return sum(
        n
        for (u, feature, _, _), n in store.rows.items()
        if u == user_id and feature == quota.CACHE_HIT_QUOTA_FEATURE
    )


def _snapshot(store: FakeQuotaStore):
    """Everything a hit must leave unchanged (the hit counter aside)."""
    return (
        {k: n for k, n in store.rows.items() if k[1] != quota.CACHE_HIT_QUOTA_FEATURE},
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
        assert stored["prompt_version"] == gateway.request_fingerprint(
            FEATURES["scan.standard"]
        )
        assert stored["ttl_days"] == 30 and stored["max_rows_per_user"] == 500
        assert stored["image_hash"] != cache.image_sha256(_JPEG)  # normalised

    def test_empty_ok_result_is_not_stored(self, client, store, fake_cache, provider):
        """An ok reply with no books may be a missing or unparseable body:
        never pinned for the TTL. A re-upload calls the provider again."""
        provider.return_value.identify = AsyncMock(return_value=ProviderResult([]))
        assert _post(client).status_code == 200
        assert fake_cache.stores == [] and fake_cache.rows == {}
        assert _post(client).status_code == 200
        assert provider.return_value.identify.await_count == 2

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
        assert _hits(store) == 1  # counted against cache.max_hits_per_day

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


class TestDailyHitCap:
    """cache.max_hits_per_day: hits up to the cap are free; past it a
    re-upload takes the normal path. The cap never refuses a scan."""

    def test_hits_up_to_the_cap_then_the_normal_path(
        self, client, store, fake_cache, provider, ledger_rows
    ):
        ai_settings.reset_cache({"cache.max_hits_per_day": 2})
        _post(client)  # miss: provider, one unit, stored
        after_miss = _snapshot(store)
        for _ in range(2):
            assert _post(client).status_code == 200
        assert provider.return_value.identify.await_count == 1
        assert _snapshot(store) == after_miss
        assert _hits(store) == 2
        # Third hit is past the cap: provider called, allowance used.
        resp = _post(client)
        assert resp.status_code == 200
        assert resp.json()["books"][0]["title"] == "Dune"
        assert provider.return_value.identify.await_count == 2
        day_units = [
            n for (u, f, k, _), n in store.rows.items() if f == "scan" and k == "day"
        ]
        assert day_units == [2]
        assert _hits(store) == 2
        assert [r["status"] for r in ledger_rows] == ["cache_hit", "cache_hit"]

    def test_cap_zero_makes_every_request_a_miss(
        self, client, store, fake_cache, provider, ledger_rows
    ):
        ai_settings.reset_cache({"cache.max_hits_per_day": 0})
        for _ in range(3):
            assert _post(client).status_code == 200
        assert provider.return_value.identify.await_count == 3
        assert ledger_rows == []
        assert _hits(store) == 0

    def test_counter_is_per_user(self, store, fake_cache, provider, ledger_rows):
        from app.auth.dependencies import get_current_user
        from app.core.database import get_db

        other = User(id=uuid.UUID("00000000-0000-0000-0000-00000000534c"), email="o@x")
        current = {"user": USER}

        async def _fake_db():
            yield AsyncMock()

        app.dependency_overrides[get_current_user] = lambda: current["user"]
        app.dependency_overrides[get_db] = _fake_db
        ai_settings.reset_cache({"cache.max_hits_per_day": 1})
        try:
            c = TestClient(app)
            _post(c)
            _post(c)  # USER: hit 1 of 1
            current["user"] = other
            _post(c)
            _post(c)  # other: own hit 1 of 1, not blocked by USER's
        finally:
            app.dependency_overrides.clear()
        assert provider.return_value.identify.await_count == 2
        assert (_hits(store), _hits(store, other.id)) == (1, 1)

    def test_counting_failure_is_a_miss(
        self, client, store, fake_cache, provider, ledger_rows
    ):
        _post(client)
        with patch(
            "app.api.scan.quota.take_cache_hit",
            AsyncMock(side_effect=RuntimeError("x")),
        ):
            assert _post(client).status_code == 200
        assert provider.return_value.identify.await_count == 2
        assert ledger_rows == []

    def test_non_premium_enhanced_refused_before_counting(
        self, client, store, fake_cache, provider, monkeypatch
    ):
        monkeypatch.setattr(
            entitlements, "tier_for", AsyncMock(return_value=Tier.PREMIUM)
        )
        _post(client, enhanced=True)
        monkeypatch.setattr(entitlements, "tier_for", AsyncMock(return_value=Tier.FREE))
        assert _post(client, enhanced=True).status_code == 403
        assert _hits(store) == 0


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

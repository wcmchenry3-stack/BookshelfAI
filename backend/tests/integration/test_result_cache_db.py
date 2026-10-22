"""The per-user scan result cache on real Postgres (#534): a hit makes no
provider call and takes nothing (allowances, lease, global count, cost), it
writes one cache_hit ledger row with zero cost, an enhanced hit writes no
zero-result row, refusals are not cached, expired rows are never served and
are purged, the per-user cap evicts oldest first, a provider/model or prompt
switch misses, rows go with the user, and the 0011 migration cycle.

Needs DATABASE_URL pointing at a Postgres database.
"""

import importlib.util
import io
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import delete, func, inspect, literal, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.datastructures import Headers
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.ai import breaker, cache, entitlements, quota
from app.ai import settings as ai_settings
from app.ai.entitlements import Tier
from app.ai.providers.base import ProviderResult
from app.api import scan as scan_api
from app.core.config import settings
from app.core.database import Base
from app.models.ai_enhanced_zero_result import AIEnhancedZeroResult
from app.models.ai_quota import AIInflight
from app.models.ai_result_cache import AIResultCache
from app.models.ai_usage import AIUsage
from app.models.user import User
from app.schemas.book import EnrichedBook
from app.services.book_identifier import BookCandidate

pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0011_ai_result_cache.py"
)
DUNE = BookCandidate(
    title="Dune", author="Frank Herbert", confidence=0.9, isbn_13="9780441013593"
)


def _jpeg(color=(10, 120, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(buf, format="JPEG")
    return buf.getvalue()


_JPEG = _jpeg()


@pytest.fixture
async def sessions():
    engine = create_async_engine(settings.async_database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    created: list[uuid.UUID] = []

    async def make_user() -> uuid.UUID:
        uid = uuid.uuid4()
        async with factory() as db:
            db.add(User(id=uid, email=f"{uid}@example.com", google_id=f"g-{uid}"))
            await db.commit()
        created.append(uid)
        return uid

    yield factory, make_user

    async with factory() as db:
        await db.execute(delete(AIUsage).where(AIUsage.user_id.in_(created)))
        await db.execute(delete(User).where(User.id.in_(created)))
        await db.commit()
    await engine.dispose()


class _Provider:
    def __init__(self, books=True, outcome="ok"):
        self.books = books
        self.outcome = outcome
        self.calls = 0

    async def identify(self, _image):
        self.calls += 1
        return ProviderResult([DUNE] if self.books else [], outcome=self.outcome)

    def patch(self, cls="OpenAIVisionProvider"):
        mock = MagicMock()
        mock.return_value.identify = self.identify
        return patch(f"app.ai.gateway.{cls}", mock)


@pytest.fixture
def downstream():
    async def enrich(candidates, limit):
        return [
            EnrichedBook(title=c.title, author=c.author, confidence=c.confidence)
            for c in candidates
        ]

    with (
        patch("app.api.scan.EnrichmentService") as enrich_cls,
        patch("app.api.scan.DeduplicationService") as dedup_cls,
        patch.object(settings, "turnstile_required", False),
    ):
        enrich_cls.return_value.enrich = AsyncMock(side_effect=enrich)
        dedup_cls.return_value.check = AsyncMock(side_effect=lambda _d, _u, b: b)
        yield


@pytest.fixture
def premium():
    with patch.object(entitlements, "tier_for", AsyncMock(return_value=Tier.PREMIUM)):
        yield


def _upload(image=_JPEG) -> StarletteUploadFile:
    return StarletteUploadFile(
        file=io.BytesIO(image),
        filename="scan.jpg",
        headers=Headers({"content-type": "image/jpeg"}),
    )


async def _scan(factory, user_id, enhanced=False, image=_JPEG):
    async with factory() as db:
        user = await db.get(User, user_id)
        return await scan_api.scan.__wrapped__(
            request=MagicMock(),
            file=_upload(image),
            current_user=user,
            db=db,
            cf_turnstile_response=None,
            enhanced=enhanced,
        )


async def _usage(factory, user_id):
    async with factory() as db:
        user = await db.get(User, user_id)
        return await quota.usage_for(
            db,
            user=user,
            feature=quota.SCAN_QUOTA_FEATURE,
            ai=await ai_settings.get_ai_settings(db),
        )


async def _state(factory, user_id):
    """Everything a hit must leave unchanged."""
    today = quota.utc_today()
    async with factory() as db:
        lease = await db.get(AIInflight, user_id)
        global_units = await quota.global_units_today(db, today)
        spend = await breaker.totals(db, today)
        zero = (
            await db.execute(
                select(func.count())
                .select_from(AIEnhancedZeroResult)
                .where(AIEnhancedZeroResult.user_id == user_id)
            )
        ).scalar_one()
    return {
        "usage": await _usage(factory, user_id),
        "lease": lease,
        "global": global_units,
        "spend": spend,
        "zero_results": zero,
    }


async def _ledger(factory, user_id) -> list[AIUsage]:
    async with factory() as db:
        return list(
            (
                await db.execute(
                    select(AIUsage)
                    .where(AIUsage.user_id == user_id)
                    .order_by(AIUsage.id)
                )
            ).scalars()
        )


async def _rows(factory, user_id) -> list[AIResultCache]:
    async with factory() as db:
        return list(
            (
                await db.execute(
                    select(AIResultCache)
                    .where(AIResultCache.user_id == user_id)
                    .order_by(AIResultCache.created_at)
                )
            ).scalars()
        )


# ---------------------------------------------------------------------------
# Hit and miss through /scan
# ---------------------------------------------------------------------------


class TestHit:
    async def test_hit_returns_without_a_provider_call(self, sessions, downstream):
        factory, make_user = sessions
        user_id = await make_user()
        provider = _Provider()
        with provider.patch():
            first = await _scan(factory, user_id)
            second = await _scan(factory, user_id)
        assert provider.calls == 1
        assert second == first
        assert [b.title for b in second.books] == ["Dune"]
        (row,) = await _rows(factory, user_id)
        assert row.expires_at - row.created_at == timedelta(days=30)

    async def test_hit_takes_nothing_and_writes_one_zero_cost_cache_hit_row(
        self, sessions, downstream
    ):
        factory, make_user = sessions
        user_id = await make_user()
        provider = _Provider()
        with provider.patch():
            await _scan(factory, user_id)
            before = await _state(factory, user_id)
            ledger_before = await _ledger(factory, user_id)
            await _scan(factory, user_id)
            after = await _state(factory, user_id)
        assert provider.calls == 1
        assert after == before
        assert after["usage"].used_today == 1 and after["usage"].used_month == 1
        assert after["lease"] is None
        ledger_after = await _ledger(factory, user_id)
        assert len(ledger_after) == len(ledger_before) + 1
        hit = ledger_after[-1]
        assert hit.status == "cache_hit"
        assert hit.cost_micros == 0
        assert (hit.input_tokens, hit.output_tokens) == (0, 0)
        assert (hit.cache_read_tokens, hit.cache_write_tokens) == (0, 0)
        assert hit.feature == "scan.standard"
        assert hit.model == ledger_before[-1].model
        assert hit.call_id != ledger_before[-1].call_id

    async def test_hit_served_at_the_daily_limit(self, sessions, downstream):
        """A hit uses no allowance, so it is served even when none is left."""
        factory, make_user = sessions
        user_id = await make_user()
        provider = _Provider()
        with provider.patch(), patch.object(settings, "scan_free_daily_limit", 1):
            await _scan(factory, user_id)
            resp = await _scan(factory, user_id)
            with pytest.raises(HTTPException) as exc:
                await _scan(factory, user_id, image=_jpeg((1, 2, 3)))
        assert [b.title for b in resp.books] == ["Dune"]
        assert exc.value.detail == "daily_scan_limit"
        assert provider.calls == 1

    async def test_enhanced_hit_writes_no_zero_result_row_and_keeps_remaining(
        self, sessions, downstream, premium
    ):
        factory, make_user = sessions
        user_id = await make_user()
        provider = _Provider()
        ai_settings.reset_cache({"quota.premium.enhanced_monthly": 20})
        with provider.patch():
            first = await _scan(factory, user_id, enhanced=True)
            before = await _state(factory, user_id)
            second = await _scan(factory, user_id, enhanced=True)
            after = await _state(factory, user_id)
        assert provider.calls == 1
        assert after == before
        assert after["zero_results"] == 0
        assert [b.title for b in second.books] == ["Dune"]
        assert second.enhanced is True
        assert second.enhanced_scans_remaining == first.enhanced_scans_remaining
        assert second.enhanced_scans_remaining == (
            20 - after["usage"].enhanced_used_month
        )

    async def test_enhanced_hit_refused_once_the_user_is_not_premium(
        self, sessions, downstream
    ):
        factory, make_user = sessions
        user_id = await make_user()
        provider = _Provider()
        with provider.patch():
            with patch.object(
                entitlements, "tier_for", AsyncMock(return_value=Tier.PREMIUM)
            ):
                await _scan(factory, user_id, enhanced=True)
            assert len(await _rows(factory, user_id)) == 1
            before = [r.call_id for r in await _ledger(factory, user_id)]
            with pytest.raises(HTTPException) as exc:
                await _scan(factory, user_id, enhanced=True)
        assert (exc.value.status_code, exc.value.detail) == (
            403,
            "enhanced_requires_premium",
        )
        assert provider.calls == 1
        assert [r.call_id for r in await _ledger(factory, user_id)] == before


class TestDailyHitCap:
    async def test_hits_up_to_the_cap_then_the_normal_path(self, sessions, downstream):
        factory, make_user = sessions
        user_id = await make_user()
        provider = _Provider()
        ai_settings.reset_cache({"cache.max_hits_per_day": 2})
        with provider.patch():
            await _scan(factory, user_id)
            before = await _state(factory, user_id)
            await _scan(factory, user_id)
            await _scan(factory, user_id)
            assert await _state(factory, user_id) == before
            assert provider.calls == 1
            resp = await _scan(factory, user_id)  # third hit: past the cap
        assert [b.title for b in resp.books] == ["Dune"]
        assert provider.calls == 2
        after = await _state(factory, user_id)
        assert after["usage"].used_today == before["usage"].used_today + 1
        assert after["global"] == before["global"] + 1
        statuses = [r.status for r in await _ledger(factory, user_id)]
        assert statuses == ["ok", "cache_hit", "cache_hit", "ok"]
        async with factory() as db:
            hits = await quota.units_used(
                db, user_id, quota.CACHE_HIT_QUOTA_FEATURE, "day", quota.utc_today()
            )
        assert hits == 2

    async def test_cap_zero_every_request_is_a_miss(self, sessions, downstream):
        factory, make_user = sessions
        user_id = await make_user()
        provider = _Provider()
        ai_settings.reset_cache({"cache.max_hits_per_day": 0})
        with provider.patch():
            for _ in range(3):
                await _scan(factory, user_id)
        assert provider.calls == 3
        assert (await _usage(factory, user_id)).used_today == 3

    async def test_counter_is_per_user(self, sessions, downstream):
        factory, make_user = sessions
        alice, bob = await make_user(), await make_user()
        provider = _Provider()
        ai_settings.reset_cache({"cache.max_hits_per_day": 1})
        with provider.patch():
            for user_id in (alice, alice, bob, bob):
                await _scan(factory, user_id)
        assert provider.calls == 2
        for user_id in (alice, bob):
            statuses = [r.status for r in await _ledger(factory, user_id)]
            assert statuses == ["ok", "cache_hit"]


class TestMiss:
    @pytest.mark.parametrize("outcome", ["refused", "truncated"])
    async def test_refusal_and_truncation_are_not_cached(
        self, sessions, downstream, outcome
    ):
        factory, make_user = sessions
        user_id = await make_user()
        provider = _Provider(outcome=outcome)
        with provider.patch():
            await _scan(factory, user_id)
            await _scan(factory, user_id)
        assert provider.calls == 2
        assert await _rows(factory, user_id) == []

    async def test_provider_switch_misses(self, sessions, downstream):
        factory, make_user = sessions
        user_id = await make_user()
        openai, claude = _Provider(), _Provider()
        with openai.patch(), claude.patch("AnthropicVisionProvider"):
            await _scan(factory, user_id)
            ai_settings.reset_cache(
                {
                    "scan.standard.provider": "anthropic",
                    "scan.standard.model": "claude-haiku-5-5",
                }
            )
            await _scan(factory, user_id)
            await _scan(factory, user_id)
        assert (openai.calls, claude.calls) == (1, 1)
        models = {r.model for r in await _rows(factory, user_id)}
        assert len(models) == 2 and "claude-haiku-5-5" in models

    async def test_model_switch_misses(self, sessions, downstream):
        factory, make_user = sessions
        user_id = await make_user()
        provider = _Provider()
        with provider.patch():
            ai_settings.reset_cache({"scan.standard.model": "gpt-4o-mini"})
            await _scan(factory, user_id)
            ai_settings.reset_cache({"scan.standard.model": "gpt-4o"})
            await _scan(factory, user_id)
        assert provider.calls == 2

    @pytest.mark.parametrize(
        "target,attr,value",
        [
            ("app.ai.providers.openai_vision", "PROMPT_VERSION", "2099-01-1"),
            ("app.ai.providers.openai_vision", "SYSTEM_PROMPT", "Other {max_books}"),
            ("app.core.config.settings", "scan_max_books", 3),
        ],
    )
    async def test_request_shape_change_misses(
        self, sessions, downstream, target, attr, value
    ):
        factory, make_user = sessions
        user_id = await make_user()
        provider = _Provider()
        with provider.patch():
            await _scan(factory, user_id)
            with patch(f"{target}.{attr}", value):
                await _scan(factory, user_id)
            await _scan(factory, user_id)  # back to the first shape: a hit
        assert provider.calls == 2
        versions = {r.prompt_version for r in await _rows(factory, user_id)}
        assert len(versions) == 2

    async def test_empty_ok_result_is_not_cached(self, sessions, downstream):
        factory, make_user = sessions
        user_id = await make_user()
        provider = _Provider(books=False)
        with provider.patch():
            await _scan(factory, user_id)
            await _scan(factory, user_id)
        assert provider.calls == 2
        assert await _rows(factory, user_id) == []

    async def test_cache_disabled_calls_the_provider_every_time(
        self, sessions, downstream
    ):
        factory, make_user = sessions
        user_id = await make_user()
        provider = _Provider()
        ai_settings.reset_cache({"cache.enabled": False})
        with provider.patch():
            await _scan(factory, user_id)
            await _scan(factory, user_id)
        assert provider.calls == 2
        assert await _rows(factory, user_id) == []


class TestCacheFailureOnRealSession:
    """A cache failure that aborts the request's transaction must not fail
    the scan: the session is rolled back (and the user reloaded) and the
    normal path runs."""

    @staticmethod
    async def _abort(db, **_kw):
        await db.execute(select(literal(1) / literal(0)))

    async def test_lookup_failure_falls_through(self, sessions, downstream):
        factory, make_user = sessions
        user_id = await make_user()
        provider = _Provider()
        with provider.patch(), patch.object(cache, "lookup", self._abort):
            resp = await _scan(factory, user_id)
        assert [b.title for b in resp.books] == ["Dune"]
        assert provider.calls == 1
        assert (await _usage(factory, user_id)).used_today == 1

    async def test_store_failure_still_returns(self, sessions, downstream):
        factory, make_user = sessions
        user_id = await make_user()
        provider = _Provider()
        with provider.patch(), patch.object(cache, "store", self._abort):
            resp = await _scan(factory, user_id)
        assert [b.title for b in resp.books] == ["Dune"]
        assert await _rows(factory, user_id) == []
        assert (await _usage(factory, user_id)).used_today == 1


# ---------------------------------------------------------------------------
# Module on real Postgres: expiry, purge, cap, idempotence, cascade
# ---------------------------------------------------------------------------


def _key(user_id, n=0, model="gpt-4o-mini"):
    return {
        "key": cache.cache_key(
            user_id=user_id,
            image_hash=f"{n:064x}",
            feature="scan.standard",
            model=model,
            prompt_version="v1",
        ),
        "user_id": user_id,
        "image_hash": f"{n:064x}",
        "feature": "scan.standard",
        "model": model,
        "prompt_version": "v1",
    }


async def _store(factory, user_id, n=0, *, now=None, max_rows=500, ttl=30):
    parts = _key(user_id, n)
    async with factory() as db:
        await cache.store(
            db,
            **parts,
            candidates=[DUNE],
            ttl_days=ttl,
            max_rows_per_user=max_rows,
            now=now,
        )
    return parts["key"]


class TestModule:
    async def test_row_past_ttl_is_not_served_and_is_purged(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        now = cache.utc_now()
        old = await _store(factory, user_id, 1, now=now - timedelta(days=30, seconds=1))
        fresh = await _store(factory, user_id, 2, now=now - timedelta(days=29))
        async with factory() as db:
            assert await cache.lookup(db, key=old, user_id=user_id, now=now) is None
            assert await cache.lookup(db, key=fresh, user_id=user_id, now=now) == [DUNE]
            # Exactly at expires_at: not served either.
            edge = now - timedelta(days=30, seconds=1) + timedelta(days=30)
            assert await cache.lookup(db, key=old, user_id=user_id, now=edge) is None
        async with factory() as db:
            purged = await cache.purge_expired(db, now=now)
        assert purged >= 1
        keys = {r.cache_key for r in await _rows(factory, user_id)}
        assert keys == {fresh}

    async def test_purge_is_bounded_per_batch(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        now = cache.utc_now()
        for n in range(5):
            await _store(factory, user_id, n, now=now - timedelta(days=31))
        with (
            patch.object(cache, "PURGE_BATCH_SIZE", 2),
            patch.object(cache, "PURGE_MAX_BATCHES", 2),
        ):
            async with factory() as db:
                assert await cache.purge_expired(db, now=now) == 4
        assert len(await _rows(factory, user_id)) == 1

    async def test_cap_evicts_oldest_first(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        other = await make_user()
        await _store(factory, other, 0)
        now = cache.utc_now()
        keys = [
            await _store(
                factory, user_id, n, now=now + timedelta(seconds=n), max_rows=3
            )
            for n in range(5)
        ]
        remaining = [r.cache_key for r in await _rows(factory, user_id)]
        assert remaining == keys[2:]
        # Another user's rows are never evicted.
        assert len(await _rows(factory, other)) == 1

    async def test_lookup_filters_on_the_user(self, sessions):
        """A row whose key matches but whose user differs is never served."""
        factory, make_user = sessions
        owner, other = await make_user(), await make_user()
        key = await _store(factory, owner, 3)
        async with factory() as db:
            assert await cache.lookup(db, key=key, user_id=owner) == [DUNE]
            assert await cache.lookup(db, key=key, user_id=other) is None

    async def test_candidate_cap_follows_scan_max_books(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        parts = _key(user_id, 4)
        many = [DUNE] * (settings.scan_max_books + 1)
        async with factory() as db:
            with pytest.raises(cache.CacheEntryError):
                await cache.store(
                    db, **parts, candidates=many, ttl_days=30, max_rows_per_user=500
                )
        async with factory() as db:
            await cache.store(
                db, **parts, candidates=many[:-1], ttl_days=30, max_rows_per_user=500
            )
        async with factory() as db:
            with patch.object(settings, "scan_max_books", 2):
                assert await cache.lookup(db, key=parts["key"], user_id=user_id) is None

    async def test_store_twice_is_one_row(self, sessions):
        """A store whose commit landed but raised, then retried, adds nothing."""
        factory, make_user = sessions
        user_id = await make_user()
        await _store(factory, user_id, 7)
        await _store(factory, user_id, 7)
        assert len(await _rows(factory, user_id)) == 1

    async def test_deleting_the_user_removes_the_rows(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        await _store(factory, user_id, 1)
        await _store(factory, user_id, 2)
        async with factory() as db:
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()
        assert await _rows(factory, user_id) == []


# ---------------------------------------------------------------------------
# Migration 0011
# ---------------------------------------------------------------------------


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0011", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_cycle(connection) -> None:
    migration = _load_migration()
    assert (migration.revision, migration.down_revision) == ("0011", "0010")
    context = MigrationContext.configure(connection)
    table = "ai_result_cache"

    def present() -> bool:
        return inspect(connection).has_table(table)

    def diffs() -> list:
        return [d for d in compare_metadata(context, Base.metadata) if table in repr(d)]

    with Operations.context(context):
        if present():
            migration.downgrade()
        assert not present()
        migration.upgrade()
        assert present()
        assert diffs() == []
        indexes = {
            i["name"]: i["column_names"] for i in inspect(connection).get_indexes(table)
        }
        assert indexes["ix_ai_result_cache_user_created"] == ["user_id", "created_at"]
        assert indexes["ix_ai_result_cache_expires_at"] == ["expires_at"]
        (fk,) = inspect(connection).get_foreign_keys(table)
        assert fk["referred_table"] == "users"
        assert fk["options"].get("ondelete") == "CASCADE"
        assert inspect(connection).get_pk_constraint(table)["constrained_columns"] == [
            "cache_key"
        ]
        migration.downgrade()
        assert not present()
        migration.upgrade()
        assert diffs() == []


async def test_migration_upgrade_check_downgrade_cycle():
    """Runs 0011 in a transaction that is rolled back (Postgres DDL is
    transactional), so the shared test database is left as it was."""
    engine = create_async_engine(settings.async_database_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with engine.connect() as conn:
            trans = await conn.begin()
            try:
                await conn.run_sync(_migration_cycle)
            finally:
                await trans.rollback()
    finally:
        await engine.dispose()

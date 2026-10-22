"""ai_spend_daily, the daily cost circuit breaker (#531), against real
PostgreSQL: concurrency at the ceiling, settle, give-backs through /scan,
Premium at the ceiling, the count backstop, alerts exactly once under
concurrency, the 90-day purge and the 0009 migration cycle.

Skipped automatically when DATABASE_URL is the dummy URL (see conftest.py).
These tests commit, so each uses its own far-past UTC day (the request clock
is patched) and its own users, and deletes both afterwards.
"""

import asyncio
import importlib.util
import io
import random
import uuid
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import delete, inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.datastructures import Headers
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.ai import breaker, entitlements, ledger, quota
from app.ai import settings as ai_settings
from app.ai.entitlements import Tier
from app.ai.providers.base import ProviderResult, ProviderTimeoutError
from app.ai.registry import get_feature
from app.api import scan as scan_api
from app.core.config import settings
from app.core.database import Base
from app.models.ai_quota import AIInflight, AIQuota
from app.models.ai_spend import AISpendDaily
from app.models.ai_usage import AIUsage
from app.models.user import User
from app.services.book_identifier import BookCandidate, ScanUnavailableError
from tests.unit._sentry_capture import captured_events

pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0009_ai_spend_daily.py"
)
EST = get_feature("scan.standard").est_cost_ceiling_micros
DUNE = BookCandidate(title="Dune", author="F", confidence=0.9)
# 1,000 * 0.15 + 500 * 0.60 + 2,000 * 0.075 = 600 micros on gpt-4o-mini
USAGE = {"input_tokens": 1_000, "output_tokens": 500, "cache_read_tokens": 2_000}
USAGE_COST = 600


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), (10, 120, 30)).save(buf, format="JPEG")
    return buf.getvalue()


_JPEG = _jpeg()


@pytest.fixture
async def env():
    engine = create_async_engine(settings.async_database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    users: list[uuid.UUID] = []
    days: list[date] = []

    async def make_user() -> uuid.UUID:
        uid = uuid.uuid4()
        async with factory() as db:
            db.add(User(id=uid, email=f"{uid}@example.com", google_id=f"g-{uid}"))
            await db.commit()
        users.append(uid)
        return uid

    def make_day() -> date:
        # A far-past day no other test uses (rows are deleted afterwards).
        while True:
            day = date(1950, 1, 1) + timedelta(days=random.randrange(15_000))
            if day not in days:
                days.append(day)
                return day

    yield factory, make_user, make_day

    async with factory() as db:
        await db.execute(delete(AISpendDaily).where(AISpendDaily.day.in_(days)))
        await db.execute(delete(AIUsage).where(AIUsage.user_id.in_(users)))
        await db.execute(delete(User).where(User.id.in_(users)))
        await db.commit()
    await engine.dispose()


async def _totals(factory, day) -> tuple[int, int, int]:
    async with factory() as db:
        return await breaker.totals(db, day)


async def _set(factory, day, reserved=0, settled=0, mask=0) -> None:
    async with factory() as db:
        await db.execute(delete(AISpendDaily).where(AISpendDaily.day == day))
        db.add(
            AISpendDaily(
                day=day,
                reserved_micros=reserved,
                settled_micros=settled,
                alerted_mask=mask,
            )
        )
        await db.commit()


async def _units(factory, user_id, day) -> int:
    async with factory() as db:
        return await quota.units_used(db, user_id, quota.SCAN_QUOTA_FEATURE, "day", day)


def _on(day: date):
    """Run the request clock on ``day`` (noon UTC)."""
    return patch.object(
        quota, "utc_now", lambda: datetime.combine(day, time(12), tzinfo=UTC)
    )


def _upload() -> StarletteUploadFile:
    return StarletteUploadFile(
        file=io.BytesIO(_JPEG),
        filename="scan.jpg",
        headers=Headers({"content-type": "image/jpeg"}),
    )


async def _call_scan(factory, user_id):
    async with factory() as db:
        user = await db.get(User, user_id)
        return await scan_api.scan.__wrapped__(
            request=MagicMock(),
            file=_upload(),
            current_user=user,
            db=db,
            cf_turnstile_response=None,
            enhanced=False,
        )


@pytest.fixture
def downstream():
    with (
        patch("app.api.scan.EnrichmentService") as enrich,
        patch("app.api.scan.DeduplicationService") as dedup,
        patch.object(settings, "turnstile_required", False),
    ):
        enrich.return_value.enrich = AsyncMock(return_value=[])
        dedup.return_value.check = AsyncMock(return_value=[])
        yield


def _provider(**kwargs):
    cls = MagicMock()
    cls.return_value.identify = AsyncMock(**kwargs)
    return patch("app.ai.gateway.OpenAIVisionProvider", cls), cls


def _ceiling(micros: int, **rows) -> None:
    ai_settings.reset_cache({"breaker.free_daily_ceiling_micros": micros, **rows})


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


class TestPrimitives:
    async def test_concurrent_free_reservations_never_pass_the_ceiling(self, env):
        """40 concurrent Free reservations of 10 under a ceiling of 95 (with
        settled spend counting too): exactly the ones that fit succeed."""
        factory, _, make_day = env
        day = make_day()
        await _set(factory, day, settled=15)

        async def one() -> bool:
            async with factory() as db:
                return await breaker.reserve(db, day, 10, 95, enforce=True)

        results = await asyncio.gather(*(one() for _ in range(40)))
        assert results.count(True) == 8  # 15 + 8 * 10 = 95
        assert await _totals(factory, day) == (80, 15, 0)

    async def test_first_reservation_of_the_day_creates_the_row(self, env):
        factory, _, make_day = env
        day = make_day()
        async with factory() as db:
            assert await breaker.reserve(db, day, 10, 10, enforce=True)
            assert not await breaker.reserve(db, day, 1, 10, enforce=True)
        assert await _totals(factory, day) == (10, 0, 0)

    async def test_premium_reservation_ignores_the_ceiling(self, env):
        factory, _, make_day = env
        day = make_day()
        await _set(factory, day, settled=100)
        async with factory() as db:
            assert await breaker.reserve(db, day, 10, 100, enforce=False)
            assert not await breaker.reserve(db, day, 10, 100, enforce=True)
        assert await _totals(factory, day) == (10, 100, 0)

    async def test_settle_lower_than_estimate_releases_the_difference(self, env):
        factory, _, make_day = env
        day = make_day()
        async with factory() as db:
            await breaker.reserve(db, day, EST, 10**9, enforce=True)
            await breaker.reserve(db, day, EST, 10**9, enforce=True)
            assert await breaker.settle(db, day, EST, 600) == (600, EST, 0)
            await db.commit()
        assert await _totals(factory, day) == (EST, 600, 0)

    async def test_release_and_settle_never_go_below_zero(self, env):
        factory, _, make_day = env
        day = make_day()
        await _set(factory, day, reserved=5)
        async with factory() as db:
            await breaker.release(db, day, EST)
            await breaker.settle(db, day, EST, 0)
            await db.commit()
        assert await _totals(factory, day) == (0, 0, 0)

    async def test_check_constraint(self, env):
        factory, _, make_day = env
        day = make_day()
        async with factory() as db:
            db.add(AISpendDaily(day=day, reserved_micros=-1))
            with pytest.raises(Exception, match="ck_ai_spend_daily_non_negative"):
                await db.commit()

    async def test_alert_bit_is_claimed_exactly_once_under_concurrency(self, env):
        factory, _, make_day = env
        day = make_day()
        await _set(factory, day, settled=950)

        async def one() -> list[int]:
            async with factory() as db:
                return await breaker.claim_alerts(db, day, (50, 75, 90))

        won = await asyncio.gather(*(one() for _ in range(20)))
        assert sorted(p for w in won for p in w) == [50, 75, 90]
        assert (await _totals(factory, day))[2] == 0b111


# ---------------------------------------------------------------------------
# Through quota.reserve and /scan
# ---------------------------------------------------------------------------


class TestScan:
    async def test_free_at_the_ceiling_gets_503_and_takes_nothing(
        self, env, downstream
    ):
        factory, make_user, make_day = env
        day = make_day()
        user_id = await make_user()
        _ceiling(1_000_000)
        await _set(factory, day, settled=1_000_000 - EST + 1)
        patch_provider, cls = _provider(return_value=ProviderResult([DUNE]))
        with _on(day), patch_provider, pytest.raises(HTTPException) as exc:
            await _call_scan(factory, user_id)
        assert (exc.value.status_code, exc.value.detail) == (503, "ai_unavailable")
        cls.assert_not_called()
        assert await _totals(factory, day) == (0, 1_000_000 - EST + 1, 0)
        assert await _units(factory, user_id, day) == 0
        async with factory() as db:
            assert await db.get(AIInflight, user_id) is None

    async def test_concurrent_free_scans_at_the_ceiling(self, env, downstream):
        """10 users scan at once with room for 3 estimates: exactly 3 reach
        the provider, the rest get ai_unavailable, and the refused users have
        no units or lease."""
        factory, make_user, make_day = env
        day = make_day()
        users = [await make_user() for _ in range(10)]
        _ceiling(3 * EST)
        gate = asyncio.Event()
        reached = []

        async def identify(_image):
            reached.append(1)
            await gate.wait()  # hold every reservation until all have tried
            return ProviderResult([], **USAGE)

        cls = MagicMock()
        cls.return_value.identify = identify

        async def one(uid) -> str:
            try:
                await _call_scan(factory, uid)
                return "ok"
            except HTTPException as exc:
                return exc.detail

        async def release_when_done(tasks):
            while sum(t.done() for t in tasks) < 7:
                await asyncio.sleep(0.01)
            gate.set()

        with _on(day), patch("app.ai.gateway.OpenAIVisionProvider", cls):
            tasks = [asyncio.create_task(one(u)) for u in users]
            await asyncio.wait_for(release_when_done(tasks), 20)
            outcomes = await asyncio.gather(*tasks)
        assert sorted(outcomes) == ["ai_unavailable"] * 7 + ["ok"] * 3
        assert len(reached) == 3
        assert await _totals(factory, day) == (0, 3 * USAGE_COST, 0)
        units = [await _units(factory, u, day) for u in users]
        assert sorted(units) == [0] * 7 + [1] * 3

    async def test_premium_at_the_ceiling_reaches_provider_and_adds_spend(
        self, env, downstream, monkeypatch
    ):
        factory, make_user, make_day = env
        day = make_day()
        user_id = await make_user()
        monkeypatch.setattr(
            entitlements, "tier_for", AsyncMock(return_value=Tier.PREMIUM)
        )
        _ceiling(1_000_000)
        await _set(factory, day, settled=1_000_000)
        patch_provider, cls = _provider(return_value=ProviderResult([], **USAGE))
        with _on(day), patch_provider:
            await _call_scan(factory, user_id)
        cls.return_value.identify.assert_awaited_once()
        assert await _totals(factory, day) == (0, 1_000_000 + USAGE_COST, 0b111)

    @pytest.mark.parametrize(
        "error", [ScanUnavailableError("down"), httpx.ConnectError("refused")]
    )
    async def test_provider_unavailable_releases_cost_and_count(
        self, env, downstream, error
    ):
        factory, make_user, make_day = env
        day = make_day()
        user_id = await make_user()
        patch_provider, _ = _provider(side_effect=error)
        with _on(day), patch_provider, pytest.raises(HTTPException) as exc:
            await _call_scan(factory, user_id)
        assert exc.value.detail == "scan_unavailable"
        assert await _totals(factory, day) == (0, 0, 0)
        assert await _units(factory, user_id, day) == 0

    async def test_other_provider_error_keeps_the_estimate_as_spend(
        self, env, downstream
    ):
        factory, make_user, make_day = env
        day = make_day()
        user_id = await make_user()
        patch_provider, _ = _provider(side_effect=RuntimeError("boom"))
        with _on(day), patch_provider, pytest.raises(RuntimeError):
            await _call_scan(factory, user_id)
        assert await _totals(factory, day) == (0, EST, 0)
        assert await _units(factory, user_id, day) == 1

    async def test_refusal_after_the_cost_reservation_gives_it_back(
        self, env, downstream
    ):
        factory, make_user, make_day = env
        day = make_day()
        user_id = await make_user()
        async with factory() as db:
            await quota.acquire_lease(db, user_id, 60)  # another scan in flight
        patch_provider, _ = _provider(return_value=ProviderResult([]))
        with _on(day), patch_provider, pytest.raises(HTTPException) as exc:
            await _call_scan(factory, user_id)
        assert exc.value.detail == "scan_in_progress"
        assert await _totals(factory, day) == (0, 0, 0)

    async def test_provider_timeout_settles_the_estimate(self, env, downstream):
        """Units are refunded, but the call reached the provider and is
        likely billed: the estimate stays as spend."""
        factory, make_user, make_day = env
        day = make_day()
        user_id = await make_user()
        patch_provider, _ = _provider(side_effect=ProviderTimeoutError("slow"))
        with _on(day), patch_provider, pytest.raises(HTTPException) as exc:
            await _call_scan(factory, user_id)
        assert exc.value.detail == "scan_unavailable"
        assert await _totals(factory, day) == (0, EST, 0)
        assert await _units(factory, user_id, day) == 0

    async def test_retried_settle_after_a_lost_commit_ack_adds_cost_once(self, env):
        """The settle runs twice with the same ledger row, the first one
        committed (as when its acknowledgement was lost and the give-back
        retried): the ledger insert conflicts, so the spend is not settled
        again."""
        factory, make_user, make_day = env
        day = make_day()
        user_id = await make_user()
        async with factory() as db:
            user = await db.get(User, user_id)
            with _on(day):
                reservation = await quota.reserve(
                    db,
                    user=user,
                    feature=quota.SCAN_QUOTA_FEATURE,
                    timeout_s=30,
                    ai=ai_settings.AISettings({}),
                    est_cost_micros=EST,
                )
            await breaker.reserve(db, day, EST, 10**9, enforce=True)  # another
            row = ledger.usage_insert(
                call_id=uuid.uuid4(),
                user_id=user_id,
                feature="scan.standard",
                model="gpt-4o-mini",
                status="ok",
                **USAGE,
            )
            settlement = breaker.Settlement(
                USAGE_COST, "scan.standard", "gpt-4o-mini", "ok"
            )
            await quota.release(db, reservation, row, settlement)
            assert await _totals(factory, day) == (EST, USAGE_COST, 0)
            await quota.release(db, reservation, row, settlement)
        # Without the gate the retry would take the other call's estimate out
        # of reserved and add the cost a second time.
        assert await _totals(factory, day) == (EST, USAGE_COST, 0)
        async with factory() as db:
            assert await db.get(AIInflight, user_id) is None
            rows = (
                await db.execute(select(AIUsage).where(AIUsage.user_id == user_id))
            ).all()
        assert len(rows) == 1

    async def test_count_backstop_refuses_even_with_cost_room(self, env, downstream):
        """The global count cap is a second, independent limit, checked
        before the cost ceiling: nothing is added to the spend."""
        factory, make_user, make_day = env
        day = make_day()
        other, user_id = await make_user(), await make_user()
        _ceiling(10**9, **{"scan.global_daily_limit": 1})
        async with factory() as db:
            db.add(
                AIQuota(
                    user_id=other,
                    feature=quota.SCAN_QUOTA_FEATURE,
                    window_kind="day",
                    window_start=day,
                    units_used=1,
                )
            )
            await db.commit()
        patch_provider, cls = _provider(return_value=ProviderResult([]))
        with _on(day), patch_provider, pytest.raises(HTTPException) as exc:
            await _call_scan(factory, user_id)
        assert (exc.value.status_code, exc.value.detail) == (503, "scan_capacity")
        cls.assert_not_called()
        assert await _totals(factory, day) == (0, 0, 0)

    async def test_spend_commits_with_the_ledger_row_and_lease_release(
        self, env, downstream
    ):
        """Just before the lease delete commits, the settle is visible on the
        request session but not on another connection."""
        factory, make_user, make_day = env
        day = make_day()
        user_id = await make_user()
        real_release = quota.release_lease
        facts = {}

        async def observe(db, uid, lease_id):
            facts["request session"] = await breaker.totals(db, day)
            async with factory() as other:
                facts["other connection"] = await breaker.totals(other, day)
            return await real_release(db, uid, lease_id)

        patch_provider, _ = _provider(return_value=ProviderResult([], **USAGE))
        with _on(day), patch_provider, patch.object(quota, "release_lease", observe):
            await _call_scan(factory, user_id)
        assert facts == {
            "request session": (0, USAGE_COST, 0),
            "other connection": (EST, 0, 0),
        }
        assert await _totals(factory, day) == (0, USAGE_COST, 0)

    async def test_alerts_sent_once_per_threshold_under_concurrent_settles(
        self, env, downstream
    ):
        """12 users settle at once just under 50 %: exactly one Sentry event
        for 50 % (none for 75/90), however many settles see it crossed."""
        factory, make_user, make_day = env
        day = make_day()
        users = [await make_user() for _ in range(12)]
        _ceiling(1_000_000)
        await _set(factory, day, settled=500_000 - USAGE_COST)
        patch_provider, _ = _provider(return_value=ProviderResult([], **USAGE))
        with _on(day), patch_provider, captured_events() as events:
            await asyncio.gather(*(_call_scan(factory, u) for u in users))
        fingerprints = [e["fingerprint"] for e in events]
        assert fingerprints == [["ai-breaker", day.isoformat(), "50"]]
        settled = 500_000 - USAGE_COST + 12 * USAGE_COST
        assert await _totals(factory, day) == (0, settled, 0b001)


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


class TestPurge:
    async def test_rows_older_than_90_days_are_purged(self, env):
        factory, _, make_day = env
        today = make_day()
        kept = today - timedelta(days=90)
        purged = today - timedelta(days=91)
        older = today - timedelta(days=400)
        env_days = [kept, purged, older]
        for d in env_days:
            await _set(factory, d, settled=1)
        try:
            async with factory() as db:
                deleted = await breaker.purge_old_spend(db, today)
            assert deleted >= 2
            async with factory() as db:
                left = set(
                    (
                        await db.execute(
                            select(AISpendDaily.day).where(
                                AISpendDaily.day.in_(env_days)
                            )
                        )
                    ).scalars()
                )
            assert left == {kept}
        finally:
            async with factory() as db:
                await db.execute(
                    delete(AISpendDaily).where(AISpendDaily.day.in_(env_days))
                )
                await db.commit()

    async def test_purge_is_bounded_per_run(self, env):
        factory, _, make_day = env
        today = make_day()
        old = [today - timedelta(days=200 + i) for i in range(5)]
        for d in old:
            await _set(factory, d)
        try:
            with (
                patch.object(breaker, "PURGE_BATCH_SIZE", 2),
                patch.object(breaker, "PURGE_MAX_BATCHES", 1),
            ):
                async with factory() as db:
                    assert await breaker.purge_old_spend(db, today) == 2
        finally:
            async with factory() as db:
                await db.execute(delete(AISpendDaily).where(AISpendDaily.day.in_(old)))
                await db.commit()


# ---------------------------------------------------------------------------
# Migration 0009
# ---------------------------------------------------------------------------


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0009", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_cycle(connection) -> None:
    migration = _load_migration()
    assert (migration.revision, migration.down_revision) == ("0009", "0008")
    context = MigrationContext.configure(connection)

    def present() -> bool:
        return inspect(connection).has_table("ai_spend_daily")

    def diffs() -> list:
        return [
            d
            for d in compare_metadata(context, Base.metadata)
            if "ai_spend_daily" in repr(d)
        ]

    with Operations.context(context):
        if present():
            migration.downgrade()
        assert not present()
        migration.upgrade()
        assert present()
        assert diffs() == []
        migration.downgrade()
        assert not present()
        migration.upgrade()
        assert diffs() == []


async def test_migration_upgrade_check_downgrade_cycle():
    """Runs 0009 in a transaction that is rolled back (Postgres DDL is
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

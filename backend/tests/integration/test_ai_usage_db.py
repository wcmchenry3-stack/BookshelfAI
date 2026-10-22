"""ai_usage / ai_usage_daily (#530) against real PostgreSQL: one ledger row per
provider outcome through /scan, committed with the lease release; the 30-day
rollup (totals preserved, idempotent, young rows untouched, bounded); the
400-day purge; ON DELETE SET NULL; and the 0008 migration cycle.

Skipped automatically when DATABASE_URL is the dummy URL (see conftest.py).
These tests commit, so each creates its own users (deleted afterwards) and
uses its own feature names and far-past timestamps, so the rollup never
touches another test's rows.
"""

import importlib.util
import io
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import delete, func, insert, inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.datastructures import Headers
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.ai import ledger, quota
from app.ai.prices import CURRENT_PRICE_VERSION
from app.ai.providers.base import ProviderResult, ProviderTimeoutError
from app.api import scan as scan_api
from app.core.config import settings
from app.core.database import Base
from app.models.ai_quota import AIInflight
from app.models.ai_usage import AIUsage, AIUsageDaily
from app.models.user import User
from app.services.book_identifier import BookCandidate, ScanUnavailableError

pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0008_ai_usage.py"
)
DUNE = BookCandidate(title="Dune", author="F", confidence=0.9)


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), (10, 120, 30)).save(buf, format="JPEG")
    return buf.getvalue()


_JPEG = _jpeg()


@pytest.fixture
async def sessions():
    engine = create_async_engine(settings.async_database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    users: list[uuid.UUID] = []
    features: list[str] = []

    async def make_user() -> uuid.UUID:
        uid = uuid.uuid4()
        async with factory() as db:
            db.add(User(id=uid, email=f"{uid}@example.com", google_id=f"g-{uid}"))
            await db.commit()
        users.append(uid)
        return uid

    def make_feature() -> str:
        name = f"test.{uuid.uuid4().hex[:12]}"
        features.append(name)
        return name

    yield factory, make_user, make_feature

    async with factory() as db:
        await db.execute(delete(AIUsage).where(AIUsage.user_id.in_(users)))
        await db.execute(delete(AIUsage).where(AIUsage.feature.in_(features)))
        await db.execute(delete(AIUsageDaily).where(AIUsageDaily.feature.in_(features)))
        await db.execute(delete(User).where(User.id.in_(users)))
        await db.commit()
    await engine.dispose()


async def _rows(factory, **where) -> list[AIUsage]:
    async with factory() as db:
        stmt = select(AIUsage).order_by(AIUsage.id)
        for col, value in where.items():
            stmt = stmt.where(getattr(AIUsage, col) == value)
        return list((await db.execute(stmt)).scalars())


async def _daily(factory, feature) -> list[AIUsageDaily]:
    async with factory() as db:
        return list(
            (
                await db.execute(
                    select(AIUsageDaily)
                    .where(AIUsageDaily.feature == feature)
                    .order_by(AIUsageDaily.day, AIUsageDaily.status)
                )
            ).scalars()
        )


def _upload() -> StarletteUploadFile:
    return StarletteUploadFile(
        file=io.BytesIO(_JPEG),
        filename="scan.jpg",
        headers=Headers({"content-type": "image/jpeg"}),
    )


async def _call_scan(factory, user_id, enhanced=False):
    async with factory() as db:
        user = await db.get(User, user_id)
        return await scan_api.scan.__wrapped__(
            request=MagicMock(),
            file=_upload(),
            current_user=user,
            db=db,
            cf_turnstile_response=None,
            enhanced=enhanced,
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
    return patch("app.ai.gateway.OpenAIVisionProvider", cls)


# ---------------------------------------------------------------------------
# One row per provider outcome
# ---------------------------------------------------------------------------

_USAGE = {"input_tokens": 1_000, "output_tokens": 500, "cache_read_tokens": 2_000}


class TestScanWritesOneRow:
    @pytest.mark.parametrize(
        ("identify", "status", "raises"),
        [
            ({"return_value": ProviderResult([DUNE], **_USAGE)}, "ok", None),
            (
                {"return_value": ProviderResult([], outcome="refused", **_USAGE)},
                "refused",
                None,
            ),
            (
                {"return_value": ProviderResult([], outcome="truncated", **_USAGE)},
                "truncated",
                None,
            ),
            ({"side_effect": RuntimeError("boom")}, "error", RuntimeError),
            ({"side_effect": ProviderTimeoutError("slow")}, "timeout", HTTPException),
            ({"side_effect": ScanUnavailableError("down")}, "refunded", HTTPException),
            ({"side_effect": httpx.ConnectError("no")}, "refunded", HTTPException),
        ],
    )
    async def test_outcome_writes_one_row(
        self, sessions, downstream, identify, status, raises
    ):
        factory, make_user, _ = sessions
        user_id = await make_user()
        with _provider(**identify):
            if raises:
                with pytest.raises(raises):
                    await _call_scan(factory, user_id)
            else:
                await _call_scan(factory, user_id)

        rows = await _rows(factory, user_id=user_id)
        assert len(rows) == 1
        row = rows[0]
        assert row.status == status
        assert (row.feature, row.model) == ("scan.standard", "gpt-4o-mini")
        assert row.price_version == CURRENT_PRICE_VERSION
        if status in ("ok", "refused", "truncated"):
            assert (row.input_tokens, row.output_tokens, row.cache_read_tokens) == (
                1_000,
                500,
                2_000,
            )
            # 1,000 * 0.15 + 500 * 0.60 + 2,000 * 0.075 = 600 micros
            assert row.cost_micros == 600
        else:
            assert (row.input_tokens, row.output_tokens, row.cost_micros) == (0, 0, 0)
        assert row.created_at is not None
        async with factory() as db:
            assert await db.get(AIInflight, user_id) is None  # lease released

    async def test_enhanced_scan_row_uses_enhanced_model_price(
        self, sessions, downstream
    ):
        factory, make_user, _ = sessions
        user_id = await make_user()
        with _provider(return_value=ProviderResult([], **_USAGE)):
            await _call_scan(factory, user_id, enhanced=True)
        (row,) = await _rows(factory, user_id=user_id)
        assert (row.feature, row.model, row.cost_micros) == (
            "scan.enhanced",
            "gpt-4o",
            10_000,
        )

    async def test_refusal_before_the_provider_writes_nothing(
        self, sessions, downstream
    ):
        factory, make_user, _ = sessions
        user_id = await make_user()
        async with factory() as db:
            await quota.acquire_lease(db, user_id, 60)  # another scan in flight
        with _provider(return_value=ProviderResult([])), pytest.raises(HTTPException):
            await _call_scan(factory, user_id)
        assert await _rows(factory, user_id=user_id) == []


# ---------------------------------------------------------------------------
# Same commit as the settle
# ---------------------------------------------------------------------------


class TestSingleCommitSettle:
    async def test_failed_settle_rolls_back_the_row_with_the_release(
        self, sessions, downstream
    ):
        """The first settle attempt fails after the row and the lease delete
        were executed but before the commit: both roll back together, and the
        retry writes exactly one row and releases the lease."""
        factory, make_user, _ = sessions
        user_id = await make_user()
        real_release = quota.release_lease
        seen = []

        async def fail_before_commit(db, uid, lease_id):
            seen.append(1)
            if len(seen) == 1:
                await db.execute(delete(AIInflight).where(AIInflight.user_id == uid))
                # Inside the same transaction, the row is visible...
                visible = await db.execute(
                    select(func.count())
                    .select_from(AIUsage)
                    .where(AIUsage.user_id == uid)
                )
                assert visible.scalar_one() == 1
                # ...then the transaction fails before it commits.
                await db.execute(select(func.sqrt(-1)))
            return await real_release(db, uid, lease_id)

        with (
            _provider(return_value=ProviderResult([DUNE], **_USAGE)),
            patch.object(quota, "release_lease", fail_before_commit),
        ):
            await _call_scan(factory, user_id)
        assert len(seen) == 2
        assert len(await _rows(factory, user_id=user_id)) == 1
        async with factory() as db:
            assert await db.get(AIInflight, user_id) is None

    async def test_row_and_lease_release_commit_together(self, sessions, downstream):
        """Seen from another connection, the row appears exactly when the
        lease disappears: never one without the other."""
        factory, make_user, _ = sessions
        user_id = await make_user()
        real_release = quota.release_lease
        observed = {}

        async def observe(db, uid, lease_id):
            # Row already executed on this session, not yet committed.
            async with factory() as other:
                observed["row_before"] = (
                    await other.execute(
                        select(func.count())
                        .select_from(AIUsage)
                        .where(AIUsage.user_id == uid)
                    )
                ).scalar_one()
                observed["lease_before"] = await other.get(AIInflight, uid) is not None
            return await real_release(db, uid, lease_id)

        with (
            _provider(return_value=ProviderResult([DUNE], **_USAGE)),
            patch.object(quota, "release_lease", observe),
        ):
            await _call_scan(factory, user_id)
        assert observed == {"row_before": 0, "lease_before": True}
        assert len(await _rows(factory, user_id=user_id)) == 1
        async with factory() as db:
            assert await db.get(AIInflight, user_id) is None

    async def test_rejected_row_does_not_fail_the_scan_or_hold_the_lease(
        self, sessions
    ):
        factory, make_user, _ = sessions
        user_id = await make_user()
        bad_row = insert(AIUsage).values(
            user_id=user_id,
            feature="scan.standard",
            model="gpt-4o-mini",
            price_version=CURRENT_PRICE_VERSION,
            input_tokens=-1,  # violates ck_ai_usage_non_negative
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            cost_micros=0,
            status="ok",
        )
        book = MagicMock()
        with (
            _provider(return_value=ProviderResult([DUNE])),
            patch.object(ledger, "usage_insert", lambda **_: bad_row),
            patch.object(settings, "turnstile_required", False),
            patch("app.api.scan.EnrichmentService") as enrich,
            patch("app.api.scan.DeduplicationService") as dedup,
            patch("app.api.scan._dedupe_enriched", lambda books: books),
            patch("app.api.scan.ScanResponse", lambda **kw: kw),
        ):
            enrich.return_value.enrich = AsyncMock(return_value=[book])
            dedup.return_value.check = AsyncMock(return_value=[book])
            resp = await _call_scan(factory, user_id)
        assert resp["books"] == [book]
        assert await _rows(factory, user_id=user_id) == []
        async with factory() as db:
            assert await db.get(AIInflight, user_id) is None


# ---------------------------------------------------------------------------
# Account deletion
# ---------------------------------------------------------------------------


async def test_deleting_a_user_nulls_user_id_and_keeps_the_row(sessions):
    factory, make_user, make_feature = sessions
    user_id = await make_user()
    feature = make_feature()
    async with factory() as db:
        await ledger.record(
            db,
            user_id=user_id,
            feature=feature,
            model="gpt-4o",
            status="ok",
            input_tokens=10,
            output_tokens=5,
        )
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()
    (row,) = await _rows(factory, feature=feature)
    assert row.user_id is None
    assert (row.input_tokens, row.output_tokens, row.cost_micros) == (10, 5, 75)


async def test_check_constraints(sessions):
    factory, _, make_feature = sessions
    feature = make_feature()
    base = {
        "feature": feature,
        "model": "gpt-4o",
        "price_version": CURRENT_PRICE_VERSION,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cost_micros": 0,
        "status": "ok",
    }
    for bad in ({"status": "weird"}, {"cost_micros": -1}, {"output_tokens": -1}):
        async with factory() as db:
            with pytest.raises(Exception, match="ck_ai_usage"):
                await db.execute(insert(AIUsage).values(**{**base, **bad}))
                await db.commit()
    async with factory() as db:
        await db.execute(insert(AIUsage).values(**{**base, "status": "cache_hit"}))
        await db.commit()


# ---------------------------------------------------------------------------
# Rollup and retention
# ---------------------------------------------------------------------------

# Far in the past, so other tests' rows are never old enough to be rolled up
# by these runs.
NOW = datetime(1990, 6, 15, 12, 0, tzinfo=UTC)


async def _seed(
    factory,
    feature,
    created_at,
    status="ok",
    n=1,
    input_tokens=100,
    output_tokens=10,
    cost_micros=350,
):
    async with factory() as db:
        for _ in range(n):
            await db.execute(
                insert(AIUsage).values(
                    user_id=None,
                    feature=feature,
                    model="gpt-4o",
                    price_version=CURRENT_PRICE_VERSION,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    cost_micros=cost_micros,
                    status=status,
                    created_at=created_at,
                )
            )
        await db.commit()


def _totals(rows):
    return (
        sum(getattr(r, "calls", 1) for r in rows),
        sum(r.input_tokens for r in rows),
        sum(r.output_tokens for r in rows),
        sum(r.cost_micros for r in rows),
    )


class TestRollup:
    async def test_rollup_then_delete_preserves_totals(self, sessions):
        factory, _, make_feature = sessions
        feature = make_feature()
        old = NOW - timedelta(days=45)
        await _seed(factory, feature, old, n=3, input_tokens=100, cost_micros=350)
        await _seed(factory, feature, old + timedelta(hours=1), status="error", n=2)
        await _seed(
            factory,
            feature,
            old - timedelta(days=1),
            n=4,
            input_tokens=7,
            cost_micros=9,
        )
        raw_before = await _rows(factory, feature=feature)

        async with factory() as db:
            rolled, _ = await ledger.rollup_and_purge(db, now=NOW)

        assert rolled >= 9
        assert await _rows(factory, feature=feature) == []
        daily = await _daily(factory, feature)
        assert _totals(daily) == _totals(raw_before)
        assert [(d.day, d.status, d.calls) for d in daily] == [
            (old.date() - timedelta(days=1), "ok", 4),
            (old.date(), "error", 2),
            (old.date(), "ok", 3),
        ]

    async def test_second_run_is_a_no_op(self, sessions):
        factory, _, make_feature = sessions
        feature = make_feature()
        await _seed(factory, feature, NOW - timedelta(days=40), n=3)
        async with factory() as db:
            await ledger.rollup_and_purge(db, now=NOW)
        first = [
            (d.day, d.status, d.calls, d.cost_micros)
            for d in await _daily(factory, feature)
        ]
        async with factory() as db:
            await ledger.rollup_and_purge(db, now=NOW)
        second = [
            (d.day, d.status, d.calls, d.cost_micros)
            for d in await _daily(factory, feature)
        ]
        assert first == second == [((NOW - timedelta(days=40)).date(), "ok", 3, 1_050)]

    async def test_later_rows_add_to_an_existing_daily_row(self, sessions):
        """A day rolled up in two runs (cutoff mid-day) is summed, not
        replaced."""
        factory, _, make_feature = sessions
        feature = make_feature()
        day_start = datetime(1990, 5, 10, tzinfo=UTC)
        await _seed(factory, feature, day_start + timedelta(hours=2), n=2)
        await _seed(factory, feature, day_start + timedelta(hours=20), n=1)
        async with factory() as db:
            # Cutoff falls at 12:00 that day: only the first two move.
            await ledger.rollup_and_purge(
                db, now=day_start + timedelta(days=30, hours=12)
            )
        assert [d.calls for d in await _daily(factory, feature)] == [2]
        assert len(await _rows(factory, feature=feature)) == 1
        async with factory() as db:
            await ledger.rollup_and_purge(db, now=NOW)
        assert [d.calls for d in await _daily(factory, feature)] == [3]
        assert await _rows(factory, feature=feature) == []

    async def test_rows_younger_than_30_days_untouched(self, sessions):
        factory, _, make_feature = sessions
        feature = make_feature()
        await _seed(factory, feature, NOW - timedelta(days=29, hours=23), n=2)
        await _seed(factory, feature, NOW - timedelta(days=1), n=1)
        async with factory() as db:
            await ledger.rollup_and_purge(db, now=NOW)
        assert len(await _rows(factory, feature=feature)) == 3
        assert await _daily(factory, feature) == []

    async def test_bounded_batches_resume_on_the_next_run(self, sessions):
        factory, _, make_feature = sessions
        feature = make_feature()
        # Batches go in id order; the fixture cleans up, so these are the
        # only raw rows old enough here.
        oldest = NOW - timedelta(days=60)
        await _seed(factory, feature, oldest, n=5)
        with (
            patch.object(ledger, "PURGE_BATCH_SIZE", 2),
            patch.object(ledger, "PURGE_MAX_BATCHES", 1),
        ):
            async with factory() as db:
                rolled, _ = await ledger.rollup_and_purge(db, now=NOW)
        assert rolled == 2
        assert len(await _rows(factory, feature=feature)) == 3
        assert [d.calls for d in await _daily(factory, feature)] == [2]
        with patch.object(ledger, "PURGE_BATCH_SIZE", 2):
            async with factory() as db:
                await ledger.rollup_and_purge(db, now=NOW)
        assert await _rows(factory, feature=feature) == []
        assert [d.calls for d in await _daily(factory, feature)] == [5]

    async def test_daily_rows_kept_400_days(self, sessions):
        factory, _, make_feature = sessions
        feature = make_feature()
        today = NOW.date()
        days = {
            "purged": today - timedelta(days=401),
            "kept": today - timedelta(days=400),
            "recent": today - timedelta(days=31),
        }
        async with factory() as db:
            for day in days.values():
                await db.execute(
                    insert(AIUsageDaily).values(
                        day=day,
                        feature=feature,
                        model="gpt-4o",
                        status="ok",
                        calls=1,
                        input_tokens=1,
                        output_tokens=1,
                        cost_micros=1,
                    )
                )
            await db.commit()
            _, purged = await ledger.rollup_and_purge(db, now=NOW)
        assert purged >= 1
        assert {d.day for d in await _daily(factory, feature)} == {
            days["kept"],
            days["recent"],
        }


# ---------------------------------------------------------------------------
# Migration 0008
# ---------------------------------------------------------------------------


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0008", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_cycle(connection) -> None:
    migration = _load_migration()
    assert (migration.revision, migration.down_revision) == ("0008", "0007")
    context = MigrationContext.configure(connection)
    tables = ("ai_usage", "ai_usage_daily")

    def present() -> set[str]:
        return {t for t in tables if inspect(connection).has_table(t)}

    def diffs() -> list:
        return [
            d
            for d in compare_metadata(context, Base.metadata)
            if any(t in repr(d) for t in tables)
        ]

    with Operations.context(context):
        if present():
            migration.downgrade()
        assert present() == set()
        migration.upgrade()
        assert present() == set(tables)
        assert diffs() == []
        fks = inspect(connection).get_foreign_keys("ai_usage")
        assert [fk["options"].get("ondelete") for fk in fks] == ["SET NULL"]
        migration.downgrade()
        assert present() == set()
        migration.upgrade()
        assert diffs() == []


async def test_migration_upgrade_check_downgrade_cycle():
    """Runs 0008 in a transaction that is rolled back (Postgres DDL is
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


def test_rollup_cutoffs():
    raw, daily = ledger.rollup_cutoffs(NOW)
    assert raw == NOW - timedelta(days=30)
    assert daily == date(1989, 5, 11)

"""The ``malformed`` ledger status (#572) against real PostgreSQL: a malformed
reply through /scan writes one ``ai_usage`` row with the real billed tokens
and cost, refunds the units and settles the priced cost; both status CHECKs
accept it; it rolls up into ``ai_usage_daily``; and the 0013 migration
cycle.

Skipped automatically when DATABASE_URL is the dummy URL (see conftest.py).
"""

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import delete, insert, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ai import ledger
from app.ai.prices import CURRENT_PRICE_VERSION
from app.core.config import settings
from app.core.database import Base
from app.models.ai_quota import AIInflight
from app.models.ai_usage import AIUsage, AIUsageDaily
from tests.limits import _harness as h

pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0013_ai_usage_malformed_status.py"
)
NOW = datetime(1991, 3, 14, 12, 0, tzinfo=UTC)


@pytest.fixture
async def limits_db():
    engine, factory = h.make_factory(pool_size=5)
    await h.create_tables(engine)
    db = h.LimitsDb(factory)
    try:
        yield db
    finally:
        await db.cleanup()
        await engine.dispose()


@pytest.fixture
def downstream():
    with h.downstream() as enrichment:
        yield enrichment


async def test_scan_writes_one_malformed_row_with_real_usage(limits_db, downstream):
    db = limits_db
    day = db.make_day()
    user_id = await db.make_user()
    h.use_settings({"scan_records.enabled": True})
    provider = h.FakeProvider("malformed")
    with h.Clock(h.at(day)).patch(), provider.patch():
        got = await h.call_scan(db.factory, user_id, enhanced=False)
    assert (got.status, got.code) == (503, "scan_unavailable")
    async with db.factory() as session:
        (row,) = (
            await session.execute(select(AIUsage).where(AIUsage.user_id == user_id))
        ).scalars()
        assert await session.get(AIInflight, user_id) is None
    assert row.status == "malformed"
    assert (row.feature, row.model) == ("scan.standard", "gpt-4o-mini")
    assert (row.input_tokens, row.output_tokens, row.cache_read_tokens) == (
        1_000,
        500,
        2_000,
    )
    # 1,000 * 0.15 + 500 * 0.60 + 2,000 * 0.075 = 600 micros, settled too.
    assert row.cost_micros == 600
    assert await db.spend(day) == (0, 600)
    counters = await db.counters(user_id, day)
    assert (counters.daily, counters.monthly) == (0, 0)


async def _engine_factory():
    engine = create_async_engine(settings.async_database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def test_check_constraints_accept_malformed_and_it_rolls_up():
    engine, factory = await _engine_factory()
    feature = f"test.{uuid.uuid4().hex[:12]}"
    try:
        async with factory() as session:
            await session.execute(
                ledger.usage_insert(
                    call_id=uuid.uuid4(),
                    user_id=None,
                    feature=feature,
                    model="gpt-4o-mini",
                    status="malformed",
                    input_tokens=1_000,
                    output_tokens=500,
                )
            )
            await session.execute(
                AIUsage.__table__.update()
                .where(AIUsage.feature == feature)
                .values(created_at=NOW - timedelta(days=31))
            )
            await session.commit()
        async with factory() as session:
            await ledger.rollup_and_purge(session, now=NOW)
        async with factory() as session:
            (daily,) = (
                await session.execute(
                    select(AIUsageDaily).where(AIUsageDaily.feature == feature)
                )
            ).scalars()
        assert (daily.status, daily.calls, daily.input_tokens) == (
            "malformed",
            1,
            1_000,
        )
        # Still a closed list.
        async with factory() as session:
            with pytest.raises(IntegrityError, match="ck_ai_usage_status"):
                await session.execute(
                    insert(AIUsage).values(
                        call_id=uuid.uuid4(),
                        feature=feature,
                        model="gpt-4o-mini",
                        price_version=CURRENT_PRICE_VERSION,
                        input_tokens=0,
                        output_tokens=0,
                        cache_read_tokens=0,
                        cache_write_tokens=0,
                        cost_micros=0,
                        status="malformedx",
                    )
                )
    finally:
        async with factory() as session:
            await session.execute(delete(AIUsage).where(AIUsage.feature == feature))
            await session.execute(
                delete(AIUsageDaily).where(AIUsageDaily.feature == feature)
            )
            await session.commit()
        await engine.dispose()


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0013", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _status_checks(connection) -> dict[str, str]:
    """The status CHECK of each ledger table, by name: its SQL text."""
    found = {}
    for table in ("ai_usage", "ai_usage_daily"):
        for check in inspect(connection).get_check_constraints(table):
            if check["name"].endswith("_status"):
                found[check["name"]] = check["sqltext"]
    return found


def _malformed_row(connection) -> None:
    connection.execute(
        insert(AIUsage).values(
            call_id=uuid.uuid4(),
            feature="test.migration-0013",
            model="gpt-4o-mini",
            price_version=CURRENT_PRICE_VERSION,
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            cost_micros=0,
            status="malformed",
        )
    )


def _migration_cycle(connection) -> None:
    migration = _load_migration()
    assert (migration.revision, migration.down_revision) == ("0013", "0012")
    context = MigrationContext.configure(connection)
    names = {"ck_ai_usage_status", "ck_ai_usage_daily_status"}
    with Operations.context(context):
        migration.downgrade()
        migration.upgrade()
        checks = _status_checks(connection)
        assert set(checks) == names
        assert all("'malformed'" in sql for sql in checks.values())
        _malformed_row(connection)
        # The downgrade keeps the existing malformed row (the old CHECK comes
        # back NOT VALID, so it does not fail on it) ...
        migration.downgrade()
        checks = _status_checks(connection)
        assert set(checks) == names
        assert not any("'malformed'" in sql for sql in checks.values())
        # ... while a new malformed row is refused again.
        with (
            pytest.raises(IntegrityError, match="ck_ai_usage_status"),
            connection.begin_nested(),
        ):
            _malformed_row(connection)
        # An upgrade after it validates every row again.
        migration.upgrade()
        assert all("'malformed'" in sql for sql in _status_checks(connection).values())
        _malformed_row(connection)


async def test_migration_upgrade_downgrade_cycle():
    """Runs 0013 in a transaction that is rolled back (Postgres DDL is
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

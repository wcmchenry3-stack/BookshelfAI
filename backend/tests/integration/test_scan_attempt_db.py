"""The ``attempt`` of each /scan request (#603) against real PostgreSQL: the
``ai_usage`` row the request writes carries it for ``ok``, ``refunded`` and a
repeat's ``cache_hit``, a missing value is stored as 1, the request is
counted (#586) and charged as before, and the 0014 migration's
upgrade -> check -> downgrade cycle (an existing row keeps ``attempt`` NULL).

Skipped automatically when DATABASE_URL is the dummy URL (see conftest.py).
"""

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.ai.prices import CURRENT_PRICE_VERSION
from app.core.config import settings
from app.core.database import Base
from app.models.ai_usage import AIUsage
from tests.limits import _harness as h

pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0014_ai_usage_attempt.py"
)


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


async def _rows(db: h.LimitsDb, user_id) -> list[tuple[str, int | None]]:
    async with db.factory() as session:
        rows = await session.execute(
            select(AIUsage.status, AIUsage.attempt)
            .where(AIUsage.user_id == user_id)
            .order_by(AIUsage.id)
        )
        return [tuple(r) for r in rows.all()]


@pytest.mark.parametrize(
    ("outcome", "attempt", "expected"),
    [
        ("books", 3, ("ok", 3)),
        ("unavailable", 6, ("refunded", 6)),
        ("books", None, ("ok", 1)),
    ],
    ids=["ok", "refunded", "missing_is_1"],
)
async def test_row_carries_the_attempt(
    limits_db, downstream, outcome, attempt, expected
):
    """The row carries the attempt; the request is counted once (#586) and
    charged as any first try (a found unit for ``ok``, none when refunded)."""
    db = limits_db
    day = db.make_day()
    user_id = await db.make_user()
    with h.Clock(h.at(day)).patch(), h.FakeProvider(outcome).patch():
        await h.call_scan(db.factory, user_id, enhanced=False, attempt=attempt)
    assert await _rows(db, user_id) == [expected]
    counters = await db.counters(user_id, day)
    assert (counters.requests, counters.daily) == (1, int(expected[0] == "ok"))


async def test_repeat_cache_hit_row_carries_its_own_attempt(limits_db, downstream):
    db = limits_db
    day = db.make_day()
    user_id = await db.make_user()
    h.use_settings({"scan_records.enabled": True})
    photo = h.distinct_jpeg()
    provider = h.FakeProvider("books")
    with h.Clock(h.at(day)).patch(), provider.patch():
        first = await h.call_scan(
            db.factory, user_id, enhanced=False, image=photo, attempt=1
        )
        second = await h.call_scan(
            db.factory, user_id, enhanced=False, image=photo, attempt=2
        )
    assert first.status == second.status == 200
    assert second.response.repeat is True
    assert provider.calls == 1
    assert await _rows(db, user_id) == [("ok", 1), ("cache_hit", 2)]


# ---------------------------------------------------------------------------
# Migration 0014
# ---------------------------------------------------------------------------


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0014", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_cycle(connection) -> None:
    migration = _load_migration()
    assert (migration.revision, migration.down_revision) == ("0014", "0013")
    context = MigrationContext.configure(connection)
    table = "ai_usage"

    def columns() -> dict:
        return {c["name"]: c for c in inspect(connection).get_columns(table)}

    def diffs() -> list:
        return [d for d in compare_metadata(context, Base.metadata) if table in repr(d)]

    with Operations.context(context):
        if "attempt" in columns():
            migration.downgrade()
        assert "attempt" not in columns()
        # An existing row survives the upgrade with attempt NULL (unknown).
        connection.execute(
            AIUsage.__table__.insert().values(
                call_id=uuid.uuid4(),
                feature="test.migration_0014",
                model="gpt-4o-mini",
                price_version=CURRENT_PRICE_VERSION,
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
                cost_micros=0,
                status="ok",
            )
        )
        migration.upgrade()
        assert diffs() == []
        column = columns()["attempt"]
        assert column["nullable"] is True
        assert column["default"] is None
        assert column["type"].__class__.__name__ == "SMALLINT"
        assert connection.execute(
            select(AIUsage.attempt).where(AIUsage.feature == "test.migration_0014")
        ).all() == [(None,)]
        migration.downgrade()
        assert "attempt" not in columns()
        assert {"id", "call_id", "status", "request_id"} <= set(columns())
        migration.upgrade()
        assert diffs() == []


async def test_migration_upgrade_check_downgrade_cycle():
    """Runs 0014 in a transaction that is rolled back (Postgres DDL is
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

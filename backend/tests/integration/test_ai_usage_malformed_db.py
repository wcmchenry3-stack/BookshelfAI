"""The ``malformed`` ledger status (#572) against real PostgreSQL: a malformed
reply through /scan writes one ``ai_usage`` row with the real billed tokens
and cost, refunds the units and settles the priced cost; both status CHECKs
accept it; it rolls up into ``ai_usage_daily``; and the 0013 migration
cycle, through real ``alembic`` commands: the downgrade keeps the rollup
working and the upgrade validates both CHECKs.

Skipped automatically when DATABASE_URL is the dummy URL (see conftest.py).
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import (
    Boolean,
    Column,
    MetaData,
    String,
    Table,
    delete,
    func,
    insert,
    select,
)
from sqlalchemy.dialects.postgresql import OID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from app.ai import ledger
from app.ai.prices import CURRENT_PRICE_VERSION
from app.core.config import settings
from app.core.database import Base
from app.models.ai_quota import AIInflight
from app.models.ai_usage import AIUsage, AIUsageDaily
from app.models.user import User
from tests.limits import _harness as h

pytestmark = pytest.mark.integration

ALEMBIC_DIR = Path(__file__).resolve().parents[2] / "alembic"
# The catalog table, for reading a CHECK's validated flag and definition.
pg_constraint = Table(
    "pg_constraint",
    MetaData(),
    Column("oid", OID),
    Column("conname", String),
    Column("convalidated", Boolean),
    schema="pg_catalog",
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


def _alembic(action: str, revision: str) -> None:
    """Run ``alembic <action> <revision>`` against the test database, as CI
    and the pre-deploy step do (``env.py`` runs its own event loop, so this
    is only called from sync code)."""
    # No ini file: env.py would otherwise run logging.config.fileConfig, which
    # disables every existing logger for the rest of the test session.
    config = Config()
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    getattr(command, action)(config, revision)


def _run(coro_fn):
    """Run one async step on a fresh engine (the tests below are sync)."""

    async def _go():
        engine = create_async_engine(settings.async_database_url)
        try:
            return await coro_fn(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    return asyncio.run(_go())


async def _revision(factory) -> str | None:
    async with factory() as session:
        conn = await session.connection()
        return await conn.run_sync(
            lambda c: MigrationContext.configure(c).get_current_revision()
        )


async def _checks(factory) -> dict[str, tuple[bool, str]]:
    """The status CHECK of each ledger table: (validated, definition)."""
    async with factory() as session:
        rows = await session.execute(
            select(
                pg_constraint.c.conname,
                pg_constraint.c.convalidated,
                func.pg_get_constraintdef(pg_constraint.c.oid),
            ).where(
                pg_constraint.c.conname.in_(
                    ["ck_ai_usage_status", "ck_ai_usage_daily_status"]
                )
            )
        )
        return {name: (valid, sql) for name, valid, sql in rows}


def _usage_row(feature: str, status: str, created_at: datetime | None = None):
    values = {
        "call_id": uuid.uuid4(),
        "feature": feature,
        "model": "gpt-4o-mini",
        "price_version": CURRENT_PRICE_VERSION,
        "input_tokens": 1_000,
        "output_tokens": 500,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cost_micros": 450,
        "status": status,
    }
    if created_at is not None:
        values["created_at"] = created_at
    return insert(AIUsage).values(**values)


async def _cleanup(factory, feature: str) -> None:
    async with factory() as session:
        await session.execute(delete(AIUsage).where(AIUsage.feature == feature))
        await session.execute(
            delete(AIUsageDaily).where(AIUsageDaily.feature == feature)
        )
        await session.commit()


def test_downgrade_keeps_malformed_rows_working_and_upgrade_validates():
    """#572 review (and Codex on PR #593): ``alembic downgrade 0012`` keeps
    both widened CHECKs, so with the old code the ``malformed`` rows written
    before it still roll up into ``ai_usage_daily`` and deleting their user
    (``ON DELETE SET NULL``) still works; the upgrade then validates both
    CHECKs again. Runs real ``alembic`` commands, so the database must be at
    head (CI runs ``alembic upgrade head`` first)."""
    head = _run(_revision)
    if head is None:
        pytest.skip("database not migrated with alembic (create_all only)")
    feature = f"test.{uuid.uuid4().hex[:12]}"
    old = NOW - timedelta(days=31)
    user_id = uuid.uuid4()

    async def _seed(factory):
        async with factory() as session:
            session.add(
                User(
                    id=user_id, email=f"{user_id}@example.com", google_id=f"g-{user_id}"
                )
            )
            await session.flush()
            await session.execute(_usage_row(feature, "malformed", old))
            await session.execute(_usage_row(feature, "ok", old))
            await session.execute(
                _usage_row(feature, "malformed").values(user_id=user_id)
            )
            await session.commit()

    async def _rollup(factory):
        async with factory() as session:
            await ledger.rollup_and_purge(session, now=NOW)
        async with factory() as session:
            daily = (
                await session.execute(
                    select(AIUsageDaily.status, AIUsageDaily.calls).where(
                        AIUsageDaily.feature == feature
                    )
                )
            ).all()
        return sorted(daily)

    async def _delete_user(factory):
        async with factory() as session:
            await session.execute(delete(User).where(User.id == user_id))
            await session.commit()
        async with factory() as session:
            rows = (
                await session.execute(
                    select(AIUsage.status, AIUsage.user_id).where(
                        AIUsage.feature == feature
                    )
                )
            ).all()
        return rows

    try:
        _run(_seed)
        _alembic("downgrade", "0012")
        checks = _run(_checks)
        # Both CHECKs stay widened and validated.
        assert all(valid and "'malformed'" in sql for valid, sql in checks.values())
        # The rollup of the kept raw rows works (the recent one stays raw).
        assert _run(_rollup) == [("malformed", 1), ("ok", 1)]
        # Deleting the user nulls user_id on its malformed row.
        assert _run(_delete_user) == [("malformed", None)]
    finally:
        _alembic("upgrade", head)
        _run(lambda factory: _cleanup(factory, feature))
        _run(lambda factory: _delete_user(factory))

    checks = _run(_checks)
    assert set(checks) == {"ck_ai_usage_status", "ck_ai_usage_daily_status"}
    assert all(valid and "'malformed'" in sql for valid, sql in checks.values())

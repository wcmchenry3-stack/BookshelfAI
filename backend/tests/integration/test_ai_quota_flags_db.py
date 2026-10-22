"""The unit flags on the lease row (#590) against real PostgreSQL: each unit
is taken with its flag in one commit and given back at most once, gated on
the flag; a lease taken over by another request carries no flags over; and
the 0012 migration's upgrade -> check -> downgrade cycle.

Skipped automatically when DATABASE_URL is the dummy URL (see conftest.py).
Each test creates its own users and deletes them afterwards; quota and lease
rows go with them via ON DELETE CASCADE.
"""

import importlib.util
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import delete, inspect, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ai import quota
from app.core.config import settings
from app.core.database import Base
from app.models.ai_quota import AIInflight, AIQuota
from app.models.user import User

pytestmark = pytest.mark.integration

FEATURE = quota.SCAN_QUOTA_FEATURE
DAY = date(2026, 10, 21)
MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0012_ai_inflight_units_taken.py"
)


@pytest.fixture
async def sessions():
    engine = create_async_engine(settings.async_database_url, pool_size=5)
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
        await db.execute(delete(User).where(User.id.in_(created)))
        await db.commit()
    await engine.dispose()


async def _units(factory, user_id) -> int | None:
    async with factory() as db:
        return (
            await db.execute(
                select(AIQuota.units_used).where(
                    AIQuota.user_id == user_id,
                    AIQuota.feature == FEATURE,
                    AIQuota.window_kind == "day",
                    AIQuota.window_start == DAY,
                )
            )
        ).scalar_one_or_none()


async def _flags(factory, user_id) -> set[str] | None:
    async with factory() as db:
        row = await db.get(AIInflight, user_id)
        if row is None:
            return None
        return {flag for flag in quota.UNIT_FLAGS if getattr(row, flag)}


async def _expire_lease(factory, user_id) -> None:
    async with factory() as db:
        await db.execute(
            update(AIInflight)
            .where(AIInflight.user_id == user_id)
            .values(lease_expires_at=quota._clock_timestamp() - timedelta(seconds=1))
        )
        await db.commit()


async def _take(db, user_id, lease_id, limit=5):
    return await quota.take_unit(
        db, user_id, lease_id, quota.FLAG_DAILY, FEATURE, "day", DAY, limit
    )


async def _give_back(db, user_id, lease_id):
    return await quota.give_back_unit(
        db, user_id, lease_id, quota.FLAG_DAILY, FEATURE, "day", DAY
    )


class TestTakeUnit:
    async def test_the_unit_and_its_flag_commit_together(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        async with factory() as db:
            lease_id = await quota.acquire_lease(db, user_id, 60)
            assert await _take(db, user_id, lease_id) == 1
        assert await _units(factory, user_id) == 1
        assert await _flags(factory, user_id) == {quota.FLAG_DAILY}

    async def test_a_refused_take_leaves_no_flag(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        async with factory() as db:
            lease_id = await quota.acquire_lease(db, user_id, 60)
            assert await _take(db, user_id, lease_id, limit=1) == 1
            await _give_back(db, user_id, lease_id)
            await quota.reserve_unit(db, user_id, FEATURE, "day", DAY, 1)
            assert await _take(db, user_id, lease_id, limit=1) is None
        assert await _units(factory, user_id) == 1
        assert await _flags(factory, user_id) == set()

    async def test_a_zero_limit_refuses_without_writing(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        async with factory() as db:
            lease_id = await quota.acquire_lease(db, user_id, 60)
            assert await _take(db, user_id, lease_id, limit=0) is None
        assert await _units(factory, user_id) is None
        assert await _flags(factory, user_id) == set()

    async def test_a_lease_taken_over_takes_nothing(self, sessions):
        """Fails closed: the stalled request's take is refused and neither the
        counter nor the new holder's flags move."""
        factory, make_user = sessions
        user_id = await make_user()
        async with factory() as db:
            stale = await quota.acquire_lease(db, user_id, 60)
        await _expire_lease(factory, user_id)
        async with factory() as db:
            assert await quota.acquire_lease(db, user_id, 60) is not None
            with pytest.raises(quota.ScanInProgress):
                await _take(db, user_id, stale)
        assert await _units(factory, user_id) is None
        assert await _flags(factory, user_id) == set()

    async def test_unknown_flag_is_rejected(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        async with factory() as db:
            lease_id = await quota.acquire_lease(db, user_id, 60)
            with pytest.raises(ValueError):
                await quota.take_unit(
                    db, user_id, lease_id, "lease_id", FEATURE, "day", DAY, 5
                )


class TestGiveBackUnit:
    async def test_gives_back_once(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        async with factory() as db:
            await quota.reserve_unit(db, user_id, FEATURE, "day", DAY, 5)  # earlier
            lease_id = await quota.acquire_lease(db, user_id, 60)
            await _take(db, user_id, lease_id)
            assert await _give_back(db, user_id, lease_id) == quota.UNIT_GIVEN_BACK
            # A retry (e.g. after a lost acknowledgement) gives nothing back.
            assert await _give_back(db, user_id, lease_id) == quota.UNIT_NOT_TAKEN
        assert await _units(factory, user_id) == 1
        assert await _flags(factory, user_id) == set()

    async def test_a_unit_never_taken_is_not_given_back(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        async with factory() as db:
            await quota.reserve_unit(db, user_id, FEATURE, "day", DAY, 5)
            lease_id = await quota.acquire_lease(db, user_id, 60)
            assert await _give_back(db, user_id, lease_id) == quota.UNIT_NOT_TAKEN
        assert await _units(factory, user_id) == 1

    async def test_the_count_never_goes_below_zero(self, sessions):
        """Flag set but ``units_used`` already 0 (e.g. reset by hand): the
        flag is cleared, nothing is decremented, no error."""
        factory, make_user = sessions
        user_id = await make_user()
        async with factory() as db:
            lease_id = await quota.acquire_lease(db, user_id, 60)
            assert await _take(db, user_id, lease_id) == 1
            await db.execute(
                update(AIQuota).where(AIQuota.user_id == user_id).values(units_used=0)
            )
            await db.commit()
            assert await _give_back(db, user_id, lease_id) == quota.UNIT_GIVEN_BACK
        assert await _units(factory, user_id) == 0
        assert await _flags(factory, user_id) == set()

    async def test_after_a_takeover_the_old_give_back_does_nothing(self, sessions):
        """The stalled request took a unit, its lease expired and another
        request took it over (the takeover resets the flags) and took its own
        unit. The old request's gated give-back finds nothing: the counter
        and the new holder's flag stay."""
        factory, make_user = sessions
        user_id = await make_user()
        async with factory() as db:
            stale = await quota.acquire_lease(db, user_id, 60)
            await _take(db, user_id, stale)
        await _expire_lease(factory, user_id)
        async with factory() as db:
            fresh = await quota.acquire_lease(db, user_id, 60)
            assert fresh is not None
            assert await _flags(factory, user_id) == set()  # reset
            await _take(db, user_id, fresh)
            assert await _give_back(db, user_id, stale) == quota.UNIT_LEASE_LOST
        assert await _units(factory, user_id) == 2
        assert await _flags(factory, user_id) == {quota.FLAG_DAILY}
        async with factory() as db:
            assert (await db.get(AIInflight, user_id)).lease_id == fresh


# ---------------------------------------------------------------------------
# Migration 0012
# ---------------------------------------------------------------------------


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0012", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_cycle(connection) -> None:
    migration = _load_migration()
    assert (migration.revision, migration.down_revision) == ("0012", "0011")
    context = MigrationContext.configure(connection)
    table = "ai_inflight"

    def columns() -> dict:
        return {c["name"]: c for c in inspect(connection).get_columns(table)}

    def diffs() -> list:
        return [d for d in compare_metadata(context, Base.metadata) if table in repr(d)]

    with Operations.context(context):
        if set(quota.UNIT_FLAGS) <= set(columns()):
            migration.downgrade()
        assert not set(quota.UNIT_FLAGS) & set(columns())
        migration.upgrade()
        assert diffs() == []
        for flag in quota.UNIT_FLAGS:
            assert columns()[flag]["nullable"] is False
            assert "false" in str(columns()[flag]["default"]).lower()
        migration.downgrade()
        assert not set(quota.UNIT_FLAGS) & set(columns())
        assert {"user_id", "lease_id", "lease_expires_at"} <= set(columns())
        migration.upgrade()
        assert diffs() == []


async def test_migration_upgrade_check_downgrade_cycle():
    """Runs 0012 in a transaction that is rolled back (Postgres DDL is
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

"""Integration tests for the daily scan caps against real PostgreSQL.

Skipped automatically when DATABASE_URL is the dummy URL (see conftest.py).
These tests commit (the reservation must be visible across connections), so
each one creates its own user and deletes it afterwards; usage rows go with it
via ON DELETE CASCADE.
"""

import asyncio
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import scan as scan_api
from app.core.config import settings
from app.core.database import AsyncSessionLocal, Base
from app.models.scan_usage import ScanUsageDaily
from app.models.user import User
from app.services import scan_quota

DAY = date(2026, 10, 21)


@pytest.fixture
async def sessions():
    engine = create_async_engine(
        settings.async_database_url, pool_size=10, max_overflow=10
    )
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


async def _count(factory, user_id: uuid.UUID, day: date = DAY) -> int | None:
    async with factory() as db:
        return (
            await db.execute(
                select(ScanUsageDaily.scans).where(
                    ScanUsageDaily.user_id == user_id, ScanUsageDaily.usage_date == day
                )
            )
        ).scalar_one_or_none()


@pytest.mark.integration
class TestScanQuotaDb:
    async def test_concurrent_reservations_never_exceed_limit(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        limit = 5

        async def one() -> int | None:
            async with factory() as db:
                return await scan_quota.reserve_scan(db, user_id, DAY, limit)

        results = await asyncio.gather(*(one() for _ in range(limit + 3)))

        granted = [r for r in results if r is not None]
        assert len(granted) == limit
        assert sorted(granted) == list(range(1, limit + 1))
        assert results.count(None) == 3
        assert await _count(factory, user_id) == limit

    async def test_refund_decrements_and_never_goes_below_zero(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        async with factory() as db:
            assert await scan_quota.reserve_scan(db, user_id, DAY, 3) == 1
            await scan_quota.refund_scan(db, user_id, DAY)
            assert await _count(factory, user_id) == 0
            await scan_quota.refund_scan(db, user_id, DAY)
            # No row for another day: refund is a no-op, not an insert.
            await scan_quota.refund_scan(db, user_id, DAY + timedelta(days=1))
        assert await _count(factory, user_id) == 0
        assert await _count(factory, user_id, DAY + timedelta(days=1)) is None

    async def test_refund_frees_a_slot_at_the_limit(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        async with factory() as db:
            assert await scan_quota.reserve_scan(db, user_id, DAY, 1) == 1
            assert await scan_quota.reserve_scan(db, user_id, DAY, 1) is None
            await scan_quota.refund_scan(db, user_id, DAY)
            assert await scan_quota.reserve_scan(db, user_id, DAY, 1) == 1

    async def test_global_sum_counts_only_that_day(self, sessions):
        factory, make_user = sessions
        a, b = await make_user(), await make_user()
        far_day = date(1999, 1, 1)  # isolated from other tests' rows
        async with factory() as db:
            for _ in range(2):
                await scan_quota.reserve_scan(db, a, far_day, 10)
            await scan_quota.reserve_scan(db, b, far_day, 10)
            await scan_quota.reserve_scan(db, b, far_day + timedelta(days=1), 10)
            assert await scan_quota.global_scans_today(db, far_day) == 3

    async def test_purge_deletes_only_rows_older_than_retention(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        today = date(2026, 10, 21)
        old = today - timedelta(days=scan_quota.RETENTION_DAYS + 1)
        boundary = today - timedelta(days=scan_quota.RETENTION_DAYS)
        async with factory() as db:
            for day in (old, boundary, today):
                await scan_quota.reserve_scan(db, user_id, day, 10)
            deleted = await scan_quota.purge_old_scan_usage(db, today=today)
        assert deleted >= 1
        assert await _count(factory, user_id, old) is None
        assert await _count(factory, user_id, boundary) == 1
        assert await _count(factory, user_id, today) == 1

    async def test_reserve_and_refund_reuse_the_request_session(self, sessions):
        """Regression: with a one-connection pool, a request session that already
        holds a connection (as get_current_user leaves it) can still reserve and
        refund — no second checkout. The commit also hands the connection back
        to the pool before the provider would be called."""
        _, make_user = sessions
        user_id = await make_user()

        engine = create_async_engine(
            settings.async_database_url, pool_size=1, max_overflow=0, pool_timeout=1
        )
        # Same session options as the app's request sessions.
        factory = async_sessionmaker(**{**AsyncSessionLocal.kw, "bind": engine})
        assert AsyncSessionLocal.kw["expire_on_commit"] is False
        try:
            async with factory() as db:
                user = await db.get(User, user_id)  # transaction open, conn held
                assert engine.pool.checkedout() == 1

                await scan_api._reserve_daily_scan(db, user, DAY)
                # Committed: connection returned during the provider call.
                assert engine.pool.checkedout() == 0
                assert user.id == user_id  # not expired by the commit

                await scan_api._refund_daily_scan(db, user, DAY)
                assert engine.pool.checkedout() == 0
        finally:
            await engine.dispose()

        async with sessions[0]() as check:
            scans = (
                await check.execute(
                    select(ScanUsageDaily.scans).where(
                        ScanUsageDaily.user_id == user_id,
                        ScanUsageDaily.usage_date == DAY,
                    )
                )
            ).scalar_one()
        assert scans == 0

    async def test_user_delete_cascades_to_usage_rows(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        async with factory() as db:
            await scan_quota.reserve_scan(db, user_id, DAY, 10)
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()
        assert await _count(factory, user_id) is None

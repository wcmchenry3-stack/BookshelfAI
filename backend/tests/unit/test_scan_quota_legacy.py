"""Unit tests for the legacy daily scan-cap helpers (app/services/scan_quota.py).

The SQL itself is exercised against Postgres in
``tests/integration/test_scan_quota_db.py``; these tests cover the pure
date maths and the decision logic around the database calls, with the
session mocked.
"""

import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.services import scan_quota

_USER = MagicMock(id=uuid.UUID("00000000-0000-0000-0000-000000000001"))
_DAY = date(2026, 3, 1)


def _result(*, scalar=None, rowcount=0):
    res = MagicMock()
    res.scalar_one.return_value = scalar
    res.scalar_one_or_none.return_value = scalar
    res.rowcount = rowcount
    return res


def _db(*results):
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=list(results))
    return db


class TestDateMaths:
    def test_utc_today_converts_from_other_timezones(self):
        # 23:30 on 1 March in UTC-5 is already 2 March in UTC.
        now = datetime(2026, 3, 1, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
        assert scan_quota.utc_today(now) == date(2026, 3, 2)

    def test_seconds_until_midnight(self):
        now = datetime(2026, 3, 1, 23, 0, 0, tzinfo=UTC)
        assert scan_quota.seconds_until_utc_midnight(now) == 3600

    def test_seconds_until_midnight_is_at_least_one(self):
        now = datetime(2026, 3, 1, 23, 59, 59, 999_999, tzinfo=UTC)
        assert scan_quota.seconds_until_utc_midnight(now) == 1

    def test_seconds_until_midnight_full_day_just_after_rollover(self):
        now = datetime(2026, 3, 1, 0, 0, 0, tzinfo=UTC)
        assert scan_quota.seconds_until_utc_midnight(now) == 86_400

    def test_seconds_until_midnight_respects_non_utc_input(self):
        # 20:00 at UTC-5 is 01:00 UTC, so 23h to the next UTC midnight.
        now = datetime(2026, 3, 1, 20, 0, tzinfo=timezone(timedelta(hours=-5)))
        assert scan_quota.seconds_until_utc_midnight(now) == 23 * 3600


class TestSqlHelpers:
    async def test_global_scans_today_returns_int(self):
        db = _db(_result(scalar=7))
        assert await scan_quota.global_scans_today(db, _DAY) == 7

    async def test_reserve_scan_returns_new_count_and_commits(self):
        db = _db(_result(scalar=3))
        assert await scan_quota.reserve_scan(db, _USER.id, _DAY, 5) == 3
        db.commit.assert_awaited_once()

    async def test_reserve_scan_returns_none_when_limit_reached(self):
        db = _db(_result(scalar=None))
        assert await scan_quota.reserve_scan(db, _USER.id, _DAY, 5) is None
        # Still commits so the connection is released.
        db.commit.assert_awaited_once()

    async def test_refund_scan_commits(self):
        db = _db(_result())
        await scan_quota.refund_scan(db, _USER.id, _DAY)
        db.execute.assert_awaited_once()
        db.commit.assert_awaited_once()


class TestPurge:
    async def test_stops_after_a_short_batch(self):
        db = _db(_result(rowcount=12))
        assert await scan_quota.purge_old_scan_usage(db, _DAY) == 12
        assert db.execute.await_count == 1

    async def test_continues_while_batches_are_full(self):
        full = scan_quota.PURGE_BATCH_SIZE
        db = _db(_result(rowcount=full), _result(rowcount=full), _result(rowcount=4))
        assert await scan_quota.purge_old_scan_usage(db, _DAY) == 2 * full + 4
        assert db.execute.await_count == 3
        assert db.commit.await_count == 3

    async def test_batches_are_bounded(self):
        full = scan_quota.PURGE_BATCH_SIZE
        db = _db(*[_result(rowcount=full)] * scan_quota.PURGE_MAX_BATCHES)
        total = await scan_quota.purge_old_scan_usage(db, _DAY)
        assert total == full * scan_quota.PURGE_MAX_BATCHES
        assert db.execute.await_count == scan_quota.PURGE_MAX_BATCHES

    async def test_none_rowcount_is_treated_as_zero(self):
        db = _db(_result(rowcount=None))
        assert await scan_quota.purge_old_scan_usage(db, _DAY) == 0

    async def test_cutoff_is_retention_days_before_today(self):
        db = _db(_result(rowcount=0))
        await scan_quota.purge_old_scan_usage(db, _DAY)
        stmt = db.execute.await_args.args[0]
        cutoff = _DAY - timedelta(days=scan_quota.RETENTION_DAYS)
        assert cutoff in stmt.compile().params.values()


class TestReserveDailyScan:
    async def test_global_ceiling_returns_503_without_reserving(self):
        db = AsyncMock()
        with (
            patch.object(scan_quota, "global_scans_today", AsyncMock(return_value=10)),
            patch.object(scan_quota, "reserve_scan", AsyncMock()) as reserve,
            patch.object(scan_quota.settings, "scan_global_daily_limit", 10),
            pytest.raises(HTTPException) as exc,
        ):
            await scan_quota.reserve_daily_scan(db, _USER, _DAY)
        assert exc.value.status_code == 503
        assert exc.value.detail == "scan_capacity"
        reserve.assert_not_awaited()

    async def test_user_limit_returns_429_with_retry_after(self):
        db = AsyncMock()
        with (
            patch.object(scan_quota, "global_scans_today", AsyncMock(return_value=0)),
            patch.object(scan_quota, "reserve_scan", AsyncMock(return_value=None)),
            patch.object(scan_quota.settings, "scan_global_daily_limit", 10),
            pytest.raises(HTTPException) as exc,
        ):
            await scan_quota.reserve_daily_scan(db, _USER, _DAY)
        assert exc.value.status_code == 429
        assert exc.value.detail == "daily_scan_limit"
        assert int(exc.value.headers["Retry-After"]) >= 1

    async def test_success_reserves_with_the_configured_per_user_limit(self):
        db = AsyncMock()
        reserve = AsyncMock(return_value=1)
        with (
            patch.object(scan_quota, "global_scans_today", AsyncMock(return_value=9)),
            patch.object(scan_quota, "reserve_scan", reserve),
            patch.object(scan_quota.settings, "scan_global_daily_limit", 10),
            patch.object(scan_quota.settings, "scan_daily_limit_per_user", 4),
        ):
            await scan_quota.reserve_daily_scan(db, _USER, _DAY)
        reserve.assert_awaited_once_with(db, _USER.id, _DAY, 4)


class TestRefundDailyScan:
    async def test_refunds_the_reservation(self):
        db = AsyncMock()
        refund = AsyncMock()
        with patch.object(scan_quota, "refund_scan", refund):
            await scan_quota.refund_daily_scan(db, _USER, _DAY)
        refund.assert_awaited_once_with(db, _USER.id, _DAY)
        db.rollback.assert_not_awaited()

    async def test_refund_failure_is_swallowed_and_rolled_back(self):
        db = AsyncMock()
        with patch.object(
            scan_quota, "refund_scan", AsyncMock(side_effect=RuntimeError("db down"))
        ):
            await scan_quota.refund_daily_scan(db, _USER, _DAY)
        db.rollback.assert_awaited_once()

    async def test_rollback_failure_is_also_swallowed(self):
        db = AsyncMock()
        db.rollback = AsyncMock(side_effect=RuntimeError("gone"))
        with patch.object(
            scan_quota, "refund_scan", AsyncMock(side_effect=RuntimeError("db down"))
        ):
            await scan_quota.refund_daily_scan(db, _USER, _DAY)
        db.rollback.assert_awaited_once()

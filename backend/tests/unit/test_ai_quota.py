"""app/ai/quota.py (#528): windows, allowances, the reserve flow's give-backs
and the SQL each primitive sends. The DB primitives are faked in memory here;
tests/integration/test_ai_quota_db.py runs them against Postgres."""

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import anyio.lowlevel
import pytest
from sqlalchemy.dialects import postgresql

from app.ai import entitlements, quota
from app.ai.entitlements import Tier
from app.ai.settings import AISettings
from app.core.config import settings
from app.models.user import User
from tests.unit._quota_fake import FakeQuotaStore

USER = User(id=uuid.UUID("00000000-0000-0000-0000-000000000528"), email="q@e.com")
FEATURE = quota.SCAN_QUOTA_FEATURE
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
TODAY = date(2026, 10, 21)
MONTH = date(2026, 10, 1)


def _sql(stmt) -> str:
    return str(
        stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}
        )
    )


@pytest.fixture
def store():
    s = FakeQuotaStore(default_user=USER.id)
    with s.installed():
        yield s


async def _reserve(ai=None, **kw):
    return await quota.reserve(
        AsyncMock(),
        user=USER,
        feature=FEATURE,
        timeout_s=60,
        ai=ai or AISettings({}),
        now=NOW,
        est_cost_micros=kw.pop("est_cost_micros", 10_000),
        **kw,
    )


class TestWindows:
    def test_utc_today_converts_to_utc(self):
        late_pacific = datetime(
            2026, 10, 21, 20, 0, tzinfo=timezone(timedelta(hours=-7))
        )
        assert quota.utc_today(late_pacific) == date(2026, 10, 22)
        early_tokyo = datetime(2026, 11, 1, 5, 0, tzinfo=timezone(timedelta(hours=9)))
        assert quota.utc_today(early_tokyo) == date(2026, 10, 31)

    def test_year_end_boundary_at_utc_midnight(self):
        """#582: only the day window is left; a year (and month) end is just
        the next UTC day."""
        before = datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)
        after = datetime(2027, 1, 1, 0, 0, 0, tzinfo=UTC)
        assert quota.utc_today(before) == date(2026, 12, 31)
        assert quota.utc_today(after) == date(2027, 1, 1)
        assert quota.seconds_until_utc_midnight(before) == 1

    def test_no_month_window_helpers_are_left(self):
        """#582: there is no monthly allowance, so nothing computes a month
        window or a monthly reset date any more."""
        for name in ("month_start", "next_month_start", "period_end"):
            assert not hasattr(quota, name)
        for name in ("MonthlyLimitReached", "EnhancedLimitReached"):
            assert not hasattr(quota, name)

    def test_seconds_until_utc_midnight(self):
        assert quota.seconds_until_utc_midnight(NOW) == 12 * 3600
        start = datetime(2026, 10, 21, 0, 0, tzinfo=UTC)
        assert quota.seconds_until_utc_midnight(start) == 86_400
        almost = datetime(2026, 10, 21, 23, 59, 59, 999_999, tzinfo=UTC)
        assert quota.seconds_until_utc_midnight(almost) == 1

    @pytest.mark.parametrize(
        "day,months,expected",
        [
            (date(2026, 10, 21), 13, date(2025, 9, 1)),
            (date(2026, 1, 5), 1, date(2025, 12, 1)),
            (date(2026, 1, 5), 13, date(2024, 12, 1)),
            (date(2026, 12, 31), 12, date(2025, 12, 1)),
        ],
    )
    def test_months_before(self, day, months, expected):
        assert quota.months_before(day, months) == expected

    def test_purge_cutoffs(self):
        assert quota.purge_cutoffs(TODAY) == (
            TODAY - timedelta(days=90),
            date(2025, 9, 1),
        )

    @pytest.mark.parametrize("timeout,expected", [(60.0, 90), (12.5, 43), (1, 31)])
    def test_lease_is_timeout_plus_30(self, timeout, expected):
        assert quota.lease_seconds(timeout) == expected


class TestAllowances:
    def test_free_defaults_are_od1(self):
        assert quota.allowance_for(AISettings({}), Tier.FREE) == quota.Allowance(5)

    def test_premium_defaults(self):
        assert quota.allowance_for(AISettings({}), Tier.PREMIUM) == quota.Allowance(30)

    def test_tier_selects_its_own_keys(self):
        ai = AISettings({"quota.free.daily": 1, "quota.premium.daily": 3})
        assert quota.allowance_for(ai, Tier.FREE) == quota.Allowance(1)
        assert quota.allowance_for(ai, Tier.PREMIUM) == quota.Allowance(3)

    def test_seeds_come_from_env_settings(self):
        with (
            patch.object(settings, "scan_free_daily_limit", 7),
            patch.object(settings, "scan_premium_daily_limit", 90),
        ):
            assert quota.allowance_for(AISettings({}), Tier.FREE).daily == 7
            assert quota.allowance_for(AISettings({}), Tier.PREMIUM).daily == 90

    async def test_free_user_gets_free_limits(self, store):
        ai = AISettings({"quota.free.daily": 1, "quota.premium.daily": 30})
        r = await _reserve(ai)
        assert r.tier is Tier.FREE and r.allowance.daily == 1
        await quota.release(AsyncMock(), r)
        with pytest.raises(quota.DailyLimitReached):
            await _reserve(ai)

    async def test_premium_user_gets_premium_limits(self, store, monkeypatch):
        monkeypatch.setattr(
            entitlements, "tier_for", AsyncMock(return_value=Tier.PREMIUM)
        )
        ai = AISettings({"quota.free.daily": 1, "quota.premium.daily": 3})
        for _ in range(3):
            r = await _reserve(ai)
            assert r.tier is Tier.PREMIUM
            await quota.release(AsyncMock(), r)
        with pytest.raises(quota.DailyLimitReached):
            await _reserve(ai)
        assert store.rows[(USER.id, FEATURE, "day", TODAY)] == 3


class TestReserveFlow:
    async def test_success_takes_lease_and_daily_only(self, store):
        """#582: one daily unit, no month-window unit."""
        r = await _reserve()
        assert (r.day, r.feature) == (TODAY, FEATURE)
        assert store.leases == {USER.id: r.lease_id}
        assert store.rows == {(USER.id, FEATURE, "day", TODAY): 1}

    async def test_global_refusal_takes_nothing(self, store):
        store.extra_global = 10
        with pytest.raises(quota.GlobalCapacityReached):
            await _reserve(AISettings({"scan.global_daily_limit": 10}))
        assert store.leases == {} and store.rows == {} and store.releases == 0

    async def test_in_progress_refusal_takes_nothing(self, store):
        store.leases[USER.id] = held = uuid.uuid4()
        with pytest.raises(quota.ScanInProgress) as exc:
            await _reserve()
        assert exc.value.retry_after == 2
        assert store.leases == {USER.id: held}  # the holder's lease untouched
        assert store.rows == {}

    async def test_expired_lease_is_taken_over(self, store):
        store.leases[USER.id] = uuid.uuid4()
        store.expire_lease(USER.id)
        r = await _reserve()
        assert store.leases == {USER.id: r.lease_id}

    async def test_daily_refusal_releases_lease(self, store):
        with pytest.raises(quota.DailyLimitReached) as exc:
            await _reserve(AISettings({"quota.free.daily": 0}))
        assert exc.value.retry_after == 12 * 3600
        assert store.leases == {}
        assert store.rows == {}

    async def test_month_rows_and_retired_keys_never_refuse(self, store):
        """#582: a month row left by the previous release (far over any old
        monthly limit) and a retired monthly key set to 0 change nothing:
        the scan is allowed and the month row is neither read nor written."""
        store.rows[(USER.id, FEATURE, "month", MONTH)] = 10**6
        store.rows[(USER.id, "scan.enhanced", "month", MONTH)] = 10**6
        ai = AISettings({"quota.free.monthly": 0, "quota.premium.enhanced_monthly": 0})
        r = await _reserve(ai)
        assert store.rows[(USER.id, FEATURE, "day", TODAY)] == 1
        assert store.rows[(USER.id, FEATURE, "month", MONTH)] == 10**6
        assert store.rows[(USER.id, "scan.enhanced", "month", MONTH)] == 10**6
        await quota.release(AsyncMock(), r)

    async def test_error_after_the_daily_take_gives_back_daily_and_lease(self, store):
        with (
            patch.object(quota, "renew_lease", AsyncMock(side_effect=OSError("db"))),
            pytest.raises(OSError),
        ):
            await _reserve()
        assert store.leases == {}
        assert store.rows == {(USER.id, FEATURE, "day", TODAY): 0}

    async def test_lease_lost_during_reservation_gives_everything_back(self, store):
        """A reservation that stalled past the lease (Codex P2 on #559): another
        request took the lease meanwhile, so this one must not call the
        provider, and must give back its unit without touching the new
        holder's lease."""
        real = store.take_unit
        taken_over = {}

        async def stall_then_reserve(
            db, user_id, lease_id, flag, feature, kind, start, limit
        ):
            got = await real(db, user_id, lease_id, flag, feature, kind, start, limit)
            if kind == "day":
                store.expire_lease(user_id)
                taken_over["id"] = await store.acquire_lease(db, user_id, 90)
            return got

        with (
            patch.object(quota, "take_unit", stall_then_reserve),
            pytest.raises(quota.ScanInProgress),
        ):
            await _reserve()
        assert store.rows == {(USER.id, FEATURE, "day", TODAY): 0}
        assert store.leases == {USER.id: taken_over["id"]}

    async def test_cancellation_during_reservation_releases_lease(self, store):
        with (
            patch.object(
                quota, "take_unit", AsyncMock(side_effect=asyncio.CancelledError())
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await _reserve()
        assert store.leases == {}

    async def test_lost_ack_on_a_take_gives_the_unit_back(self, store):
        """#590: the take commits (unit and flag) and its acknowledgement is
        lost. The flag shows it landed, so the unit is given back."""
        real = store.take_unit

        async def ack_lost(db, user_id, lease_id, flag, feature, kind, start, limit):
            await real(db, user_id, lease_id, flag, feature, kind, start, limit)
            raise OSError("commit acknowledgement lost")

        with patch.object(quota, "take_unit", ack_lost), pytest.raises(OSError):
            await _reserve()
        assert store.count() == 0
        assert store.leases == {}
        assert not any(kind == "month" for (_, _, kind, _) in store.rows)

    async def test_a_refused_take_gives_nothing_back(self, store):
        """A refusal that was acknowledged took nothing, so no give-back runs
        for it."""
        store.rows[(USER.id, FEATURE, "day", TODAY)] = 5
        calls = []
        real = store.give_back_unit

        async def give_back(db, user_id, lease_id, flag, feature, kind, start):
            calls.append(flag)
            return await real(db, user_id, lease_id, flag, feature, kind, start)

        with (
            patch.object(quota, "give_back_unit", give_back),
            pytest.raises(quota.DailyLimitReached),
        ):
            await _reserve(AISettings({"quota.free.daily": 5}))
        assert calls == []
        assert store.rows == {(USER.id, FEATURE, "day", TODAY): 5}
        assert store.leases == {}

    async def test_refund_gives_back_the_daily_unit_never_below_zero(self, store):
        r = await _reserve()
        db = AsyncMock()
        await quota.refund(db, r)
        await quota.refund(db, r)  # a second refund cannot go below zero
        assert set(store.rows.values()) == {0}
        assert store.leases == {USER.id: r.lease_id}  # refund leaves the lease
        await quota.release(db, r)
        assert store.leases == {}

    async def test_release_of_a_lease_taken_over_is_a_no_op(self, store):
        old = await _reserve()
        store.expire_lease(USER.id)
        new = await _reserve()
        await quota.release(AsyncMock(), old)
        assert store.leases == {USER.id: new.lease_id}


class TestGiveBack:
    async def test_failure_is_retried_after_rollback_then_swallowed(self, caplog):
        db = AsyncMock()
        op = AsyncMock(side_effect=OSError("db down"))
        await quota._give_back(db, "lease release", op)
        assert op.await_count == 2
        assert db.rollback.await_count == 2
        assert "lease release failed" in caplog.text

    async def test_second_attempt_can_succeed(self):
        db = AsyncMock()
        op = AsyncMock(side_effect=[OSError("blip"), None])
        await quota._give_back(db, "refund", op)
        assert op.await_count == 2

    async def test_runs_to_completion_inside_a_cancelled_scope(self):
        """The enclosing scope stays cancelled for the whole give-back, so
        every await inside ``op`` would raise without the shield."""
        steps = []

        async def op():
            steps.append("started")
            await anyio.lowlevel.checkpoint()
            steps.append("checkpointed")
            await anyio.sleep(0.01)
            steps.append("done")

        with anyio.CancelScope() as scope:
            scope.cancel()
            await quota._give_back(AsyncMock(), "lease release", op)
            # The caller's cancellation is still delivered afterwards.
            await anyio.lowlevel.checkpoint()
            steps.append("not cancelled")
        assert steps == ["started", "checkpointed", "done"]
        assert scope.cancelled_caught

    async def test_timeout_rolls_the_session_back(self, caplog):
        """A give-back cut off by the bound may leave the connection
        invalidated mid-query; the session is rolled back so the request can
        keep using it."""
        db = AsyncMock()

        async def op():
            await anyio.sleep(5)

        with patch.object(quota, "CLEANUP_TIMEOUT_SECONDS", 0.01):
            await quota._give_back(db, "lease release", op)
        db.rollback.assert_awaited_once()
        assert "lease release timed out" in caplog.text

    async def test_failed_rollback_after_timeout_is_swallowed(self, caplog):
        db = AsyncMock()
        db.rollback.side_effect = OSError("connection gone")

        async def op():
            await anyio.sleep(5)

        with patch.object(quota, "CLEANUP_TIMEOUT_SECONDS", 0.01):
            await quota._give_back(db, "refund", op)
        db.rollback.assert_awaited_once()
        assert "rollback after timed-out refund" in caplog.text


class TestSql:
    """The statements the primitives send (Postgres dialect)."""

    async def test_acquire_is_a_conditional_upsert_on_expiry(self):
        db = AsyncMock()
        db.execute.return_value = MagicMock(scalar_one_or_none=lambda: None)
        assert await quota.acquire_lease(db, USER.id, 90) is None
        sql = _sql(db.execute.await_args.args[0])
        assert "INSERT INTO ai_inflight" in sql
        assert "ON CONFLICT (user_id) DO UPDATE" in sql
        assert "WHERE ai_inflight.lease_expires_at < clock_timestamp()" in sql
        assert "RETURNING ai_inflight.lease_id" in sql
        db.commit.assert_awaited_once()

    async def test_acquire_returns_the_new_lease_id_only_when_it_won(self):
        db = AsyncMock()
        won = {}

        async def execute(stmt):
            won["id"] = stmt.compile().params["lease_id"]
            return MagicMock(scalar_one_or_none=lambda: won["id"])

        db.execute = execute
        assert await quota.acquire_lease(db, USER.id, 90) == won["id"]

    async def test_release_matches_the_lease_id(self):
        db = AsyncMock()
        db.execute.return_value = MagicMock(rowcount=0)
        assert await quota.release_lease(db, USER.id, uuid.uuid4()) is False
        sql = _sql(db.execute.await_args.args[0])
        assert "DELETE FROM ai_inflight" in sql
        assert "ai_inflight.lease_id =" in sql

    async def test_reserve_is_the_509_conditional_upsert(self):
        db = AsyncMock()
        db.execute.return_value = MagicMock(scalar_one_or_none=lambda: 1)
        assert await quota.reserve_unit(db, USER.id, FEATURE, "day", TODAY, 5) == 1
        sql = _sql(db.execute.await_args.args[0])
        assert (
            "ON CONFLICT (user_id, feature, window_kind, window_start) DO UPDATE" in sql
        )
        assert "units_used = (ai_quota.units_used +" in sql
        assert "WHERE ai_quota.units_used <" in sql

    async def test_zero_limit_refuses_without_touching_the_db(self):
        db = AsyncMock()
        assert await quota.reserve_unit(db, USER.id, FEATURE, "day", TODAY, 0) is None
        db.execute.assert_not_awaited()

    async def test_refund_never_goes_below_zero(self):
        db = AsyncMock()
        await quota.refund_unit(db, USER.id, FEATURE, "month", MONTH)
        sql = _sql(db.execute.await_args.args[0])
        assert sql.startswith("UPDATE ai_quota SET units_used=(ai_quota.units_used -")
        assert "ai_quota.units_used >" in sql

    async def test_purge_deletes_in_bounded_batches_per_kind(self):
        db = AsyncMock()
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(rowcount=quota.PURGE_BATCH_SIZE),
                MagicMock(rowcount=3),
                MagicMock(rowcount=4),
            ]
        )
        assert await quota.purge_old_quota(db, today=TODAY) == (
            quota.PURGE_BATCH_SIZE + 7
        )
        assert db.execute.await_count == 3
        assert "ai_quota" in _sql(db.execute.await_args.args[0])

    async def test_purge_stops_at_max_batches(self):
        db = AsyncMock()
        db.execute = AsyncMock(return_value=MagicMock(rowcount=quota.PURGE_BATCH_SIZE))
        await quota.purge_old_quota(db, today=TODAY)
        assert db.execute.await_count == 2 * quota.PURGE_MAX_BATCHES

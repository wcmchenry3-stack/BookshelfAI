"""ai_quota / ai_inflight (#528) against real PostgreSQL: exact reservations
under concurrency, the one-call-in-flight lease, refunds, the purge and the
0007 migration's upgrade -> check -> downgrade cycle.

Skipped automatically when DATABASE_URL is the dummy URL (see conftest.py).
These tests commit (reservations and leases must be visible across
connections), so each one creates its own users and deletes them afterwards;
quota and lease rows go with them via ON DELETE CASCADE.
"""

import asyncio
import importlib.util
import io
import uuid
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import delete, func, inspect, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.datastructures import Headers
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.ai import quota
from app.ai.providers.base import ProviderResult
from app.ai.settings import AISettings
from app.api import scan as scan_api
from app.core.config import settings
from app.core.database import AsyncSessionLocal, Base
from app.models.ai_quota import AIInflight, AIQuota
from app.models.user import User
from app.services.book_identifier import BookCandidate, ScanUnavailableError

pytestmark = pytest.mark.integration

# Every scan, standard or enhanced, reserves under this one key (OD-1).
FEATURE = quota.SCAN_QUOTA_FEATURE
EST = 10_000  # a scan.standard-sized cost estimate (#531)
# Another feature's counter (the column allows later AI features their own).
OTHER_FEATURE = "other.feature"
DAY = date(2026, 10, 21)
MONTH = date(2026, 10, 1)
MIGRATION = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0007_ai_quota.py"
)


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), (10, 120, 30)).save(buf, format="JPEG")
    return buf.getvalue()


_JPEG = _jpeg()


@pytest.fixture
async def sessions():
    engine = create_async_engine(
        settings.async_database_url, pool_size=25, max_overflow=10
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


async def _units(factory, user_id, kind="day", start=DAY, feature=FEATURE):
    async with factory() as db:
        return (
            await db.execute(
                select(AIQuota.units_used).where(
                    AIQuota.user_id == user_id,
                    AIQuota.feature == feature,
                    AIQuota.window_kind == kind,
                    AIQuota.window_start == start,
                )
            )
        ).scalar_one_or_none()


async def _lease(factory, user_id) -> AIInflight | None:
    async with factory() as db:
        return await db.get(AIInflight, user_id)


async def _expire_lease(factory, user_id) -> None:
    async with factory() as db:
        await db.execute(
            update(AIInflight)
            .where(AIInflight.user_id == user_id)
            .values(lease_expires_at=quota._clock_timestamp() - timedelta(seconds=1))
        )
        await db.commit()


def _upload() -> StarletteUploadFile:
    return StarletteUploadFile(
        file=io.BytesIO(_JPEG),
        filename="scan.jpg",
        headers=Headers({"content-type": "image/jpeg"}),
    )


async def _call_scan(factory, user_id, enhanced=False):
    """Call the /scan handler below the rate limiter with a real session."""
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
    """Mock enrichment and dedup; the provider is patched per test."""
    with (
        patch("app.api.scan.EnrichmentService") as enrich,
        patch("app.api.scan.DeduplicationService") as dedup,
        patch.object(settings, "turnstile_required", False),
    ):
        enrich.return_value.enrich = AsyncMock(return_value=[])
        dedup.return_value.check = AsyncMock(return_value=[])
        yield


class _CountingProvider:
    """Counts provider calls and the most that were ever in flight at once."""

    def __init__(self, delay: float = 0.05, error: Exception | None = None):
        self.calls = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self.delay = delay
        self.error = error

    async def identify(self, _image: bytes):
        self.calls += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            if self.error is not None:
                raise self.error
            return ProviderResult(
                [BookCandidate(title="Dune", author="F", confidence=0.9)]
            )
        finally:
            self.in_flight -= 1

    def patch(self):
        cls = MagicMock()
        cls.return_value.identify = self.identify
        return patch("app.ai.gateway.OpenAIVisionProvider", cls)


class TestReservations:
    async def test_20_concurrent_reserves_limit_5_exactly_5_succeed(self, sessions):
        """The reservation upsert alone (lease bypassed) is exact."""
        factory, make_user = sessions
        user_id = await make_user()

        async def one():
            async with factory() as db:
                return await quota.reserve_unit(db, user_id, FEATURE, "day", DAY, 5)

        results = await asyncio.gather(*(one() for _ in range(20)))
        granted = [r for r in results if r is not None]
        assert sorted(granted) == [1, 2, 3, 4, 5]
        assert results.count(None) == 15
        assert await _units(factory, user_id) == 5

    async def test_zero_limit_refuses_without_writing(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        async with factory() as db:
            assert await quota.reserve_unit(db, user_id, FEATURE, "day", DAY, 0) is None
        assert await _units(factory, user_id) is None

    async def test_refund_never_goes_below_zero(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        async with factory() as db:
            await quota.reserve_unit(db, user_id, FEATURE, "month", MONTH, 3)
            for _ in range(3):
                await quota.refund_unit(db, user_id, FEATURE, "month", MONTH)
            # No row for that window: a no-op, not an insert.
            await quota.refund_unit(db, user_id, FEATURE, "day", DAY)
        assert await _units(factory, user_id, "month", MONTH) == 0
        assert await _units(factory, user_id) is None

    async def test_features_and_windows_are_counted_separately(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        async with factory() as db:
            assert await quota.reserve_unit(db, user_id, FEATURE, "day", DAY, 1) == 1
            assert await quota.reserve_unit(db, user_id, FEATURE, "day", DAY, 1) is None
            assert (
                await quota.reserve_unit(db, user_id, OTHER_FEATURE, "day", DAY, 1) == 1
            )
            assert (
                await quota.reserve_unit(db, user_id, FEATURE, "month", MONTH, 1) == 1
            )

    async def test_global_count_sums_day_rows_of_that_day_only(self, sessions):
        factory, make_user = sessions
        a, b = await make_user(), await make_user()
        far = date(1999, 1, 1)  # isolated from other tests' rows
        async with factory() as db:
            await quota.reserve_unit(db, a, FEATURE, "day", far, 10)
            await quota.reserve_unit(db, a, OTHER_FEATURE, "day", far, 10)
            await quota.reserve_unit(db, b, FEATURE, "day", far, 10)
            await quota.reserve_unit(db, b, FEATURE, "month", far, 10)
            await quota.reserve_unit(db, b, FEATURE, "day", far + timedelta(1), 10)
            assert await quota.global_units_today(db, far) == 3

    async def test_check_constraints(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        for values in (
            {"window_kind": "week", "units_used": 1},
            {"window_kind": "day", "units_used": -1},
        ):
            async with factory() as db:
                db.add(
                    AIQuota(
                        user_id=user_id,
                        feature=FEATURE,
                        window_start=DAY,
                        **values,
                    )
                )
                with pytest.raises(Exception, match="ck_ai_quota"):
                    await db.commit()

    async def test_user_delete_cascades(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        async with factory() as db:
            await quota.reserve_unit(db, user_id, FEATURE, "day", DAY, 5)
            await quota.acquire_lease(db, user_id, 60)
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()
        assert await _units(factory, user_id) is None
        assert await _lease(factory, user_id) is None


class TestLease:
    async def test_second_acquire_while_held_is_refused(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        async with factory() as db:
            first = await quota.acquire_lease(db, user_id, 60)
            assert first is not None
            assert await quota.acquire_lease(db, user_id, 60) is None
        assert (await _lease(factory, user_id)).lease_id == first

    async def test_concurrent_acquires_only_one_wins(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()

        async def one():
            async with factory() as db:
                return await quota.acquire_lease(db, user_id, 60)

        results = await asyncio.gather(*(one() for _ in range(20)))
        assert len([r for r in results if r is not None]) == 1

    async def test_lease_length_is_timeout_plus_30(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        async with factory() as db:
            await quota.acquire_lease(db, user_id, quota.lease_seconds(60.0))
            now = (await db.execute(select(quota._clock_timestamp()))).scalar_one()
        lease = await _lease(factory, user_id)
        remaining = (lease.lease_expires_at - now).total_seconds()
        assert 88 <= remaining <= 90

    async def test_expired_lease_is_reclaimed(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        async with factory() as db:
            old = await quota.acquire_lease(db, user_id, 60)
        await _expire_lease(factory, user_id)
        async with factory() as db:
            new = await quota.acquire_lease(db, user_id, 60)
        assert new is not None and new != old
        assert (await _lease(factory, user_id)).lease_id == new

    async def test_slow_request_does_not_release_the_new_holders_lease(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        async with factory() as db:
            slow = await quota.acquire_lease(db, user_id, 60)
        await _expire_lease(factory, user_id)
        async with factory() as db:
            fresh = await quota.acquire_lease(db, user_id, 60)
            # The slow request finally settles: its release must be a no-op.
            assert await quota.release_lease(db, user_id, slow) is False
        assert (await _lease(factory, user_id)).lease_id == fresh
        async with factory() as db:
            assert await quota.release_lease(db, user_id, fresh) is True
        assert await _lease(factory, user_id) is None

    async def test_renew_restarts_the_clock_of_an_aged_lease(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        async with factory() as db:
            lease_id = await quota.acquire_lease(db, user_id, 5)
            assert await quota.renew_lease(db, user_id, lease_id, 90) is True
            now = (await db.execute(select(quota._clock_timestamp()))).scalar_one()
        lease = await _lease(factory, user_id)
        assert lease.lease_id == lease_id
        assert 88 <= (lease.lease_expires_at - now).total_seconds() <= 90

    async def test_renew_of_a_lease_taken_over_is_refused(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        async with factory() as db:
            slow = await quota.acquire_lease(db, user_id, 60)
        await _expire_lease(factory, user_id)
        async with factory() as db:
            fresh = await quota.acquire_lease(db, user_id, 60)
        before = (await _lease(factory, user_id)).lease_expires_at
        async with factory() as db:
            assert await quota.renew_lease(db, user_id, slow, 600) is False
        after = await _lease(factory, user_id)
        assert (after.lease_id, after.lease_expires_at) == (fresh, before)

    async def test_reserve_and_release_reuse_the_request_session(self, sessions):
        """With a one-connection pool, a request session that already holds a
        connection can reserve and release (no second checkout), and every
        step commits, so no connection is held during the provider call."""
        _, make_user = sessions
        user_id = await make_user()
        engine = create_async_engine(
            settings.async_database_url, pool_size=1, max_overflow=0, pool_timeout=1
        )
        factory = async_sessionmaker(**{**AsyncSessionLocal.kw, "bind": engine})
        try:
            async with factory() as db:
                user = await db.get(User, user_id)
                assert engine.pool.checkedout() == 1
                reservation = await quota.reserve(
                    db,
                    user=user,
                    feature=FEATURE,
                    timeout_s=30,
                    ai=AISettings({}),
                    est_cost_micros=EST,
                )
                assert engine.pool.checkedout() == 0
                assert user.id == user_id  # not expired by the commits
                await quota.refund(db, reservation)
                await quota.release(db, reservation)
                assert engine.pool.checkedout() == 0
        finally:
            await engine.dispose()
        today = quota.utc_today()
        assert await _units(sessions[0], user_id, "day", today) == 0
        assert await _lease(sessions[0], user_id) is None


class TestReserveFlow:
    async def test_monthly_refusal_refunds_daily_and_releases_lease(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        ai = AISettings({"quota.free.daily": 5, "quota.free.monthly": 2})
        now = quota.utc_now()
        today, month = quota.utc_today(now), quota.month_start(quota.utc_today(now))
        async with factory() as db:
            user = await db.get(User, user_id)
            for _ in range(2):
                r = await quota.reserve(
                    db,
                    user=user,
                    feature=FEATURE,
                    timeout_s=30,
                    ai=ai,
                    now=now,
                    est_cost_micros=EST,
                )
                await quota.release(db, r)
            with pytest.raises(quota.MonthlyLimitReached) as exc:
                await quota.reserve(
                    db,
                    user=user,
                    feature=FEATURE,
                    timeout_s=30,
                    ai=ai,
                    now=now,
                    est_cost_micros=EST,
                )
        assert (exc.value.limit, exc.value.used) == (2, 2)
        assert exc.value.period_end == quota.next_month_start(today)
        assert await _units(factory, user_id, "day", today) == 2
        assert await _units(factory, user_id, "month", month) == 2
        assert await _lease(factory, user_id) is None

    async def test_lease_lost_during_reservation_refuses_and_gives_back(self, sessions):
        """Codex P2 on #559: the upserts stalled past the lease and another
        request took it, so reserve must refuse instead of letting two
        provider calls run, and give back both units."""
        factory, make_user = sessions
        user_id = await make_user()
        today = quota.utc_today()
        real = quota.reserve_unit
        taken_over = {}

        async def stall_then_reserve(db, uid, feature, kind, start, limit):
            got = await real(db, uid, feature, kind, start, limit)
            if kind == "month":
                await _expire_lease(factory, uid)
                async with factory() as other:
                    taken_over["id"] = await quota.acquire_lease(other, uid, 60)
            return got

        async with factory() as db:
            user = await db.get(User, user_id)
            with (
                patch.object(quota, "reserve_unit", stall_then_reserve),
                pytest.raises(quota.ScanInProgress),
            ):
                await quota.reserve(
                    db,
                    user=user,
                    feature=FEATURE,
                    timeout_s=30,
                    ai=AISettings({}),
                    est_cost_micros=EST,
                )
        assert await _units(factory, user_id, "day", today) == 0
        assert await _units(factory, user_id, "month", quota.month_start(today)) == 0
        assert (await _lease(factory, user_id)).lease_id == taken_over["id"]

    async def test_daily_refusal_releases_lease(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        ai = AISettings({"quota.free.daily": 0})
        async with factory() as db:
            user = await db.get(User, user_id)
            with pytest.raises(quota.DailyLimitReached):
                await quota.reserve(
                    db,
                    user=user,
                    feature=FEATURE,
                    timeout_s=30,
                    ai=ai,
                    est_cost_micros=EST,
                )
        assert await _lease(factory, user_id) is None
        assert await _units(factory, user_id, "day", quota.utc_today()) is None


class TestScanEndpoint:
    async def test_concurrent_scans_one_in_flight_and_never_over_limit(
        self, sessions, downstream
    ):
        """20 concurrent /scan calls for one user (daily limit 5), each retrying
        while another holds the lease: exactly 5 provider calls, never more
        than one in flight, and the other 15 end with daily_scan_limit."""
        factory, make_user = sessions
        user_id = await make_user()
        provider = _CountingProvider()

        async def one() -> str:
            for _ in range(500):
                try:
                    await _call_scan(factory, user_id)
                    return "ok"
                except HTTPException as exc:
                    if exc.detail != "scan_in_progress":
                        return exc.detail
                    assert exc.headers == {"Retry-After": "2"}
                await asyncio.sleep(0.005)
            return "gave up"

        with (
            provider.patch(),
            patch.object(settings, "scan_free_daily_limit", 5),
            patch.object(settings, "scan_free_monthly_limit", 100),
        ):
            outcomes = await asyncio.gather(*(one() for _ in range(20)))

        assert provider.calls == 5
        assert provider.max_in_flight == 1
        assert outcomes.count("ok") == 5
        assert outcomes.count("daily_scan_limit") == 15
        assert await _units(factory, user_id, "day", quota.utc_today()) == 5
        assert await _lease(factory, user_id) is None

    async def test_concurrent_burst_without_retry_has_one_provider_call(
        self, sessions, downstream
    ):
        factory, make_user = sessions
        user_id = await make_user()
        provider = _CountingProvider(delay=0.3)

        async def one():
            try:
                await _call_scan(factory, user_id)
                return "ok"
            except HTTPException as exc:
                return exc.detail

        with provider.patch():
            outcomes = await asyncio.gather(*(one() for _ in range(20)))
        assert provider.calls == outcomes.count("ok")
        assert provider.max_in_flight == 1
        assert set(outcomes) <= {"ok", "scan_in_progress"}
        assert provider.calls <= 5

    async def test_scan_while_another_in_flight_refused_before_provider(
        self, sessions, downstream
    ):
        factory, make_user = sessions
        user_id = await make_user()
        async with factory() as db:
            await quota.acquire_lease(db, user_id, 60)  # the first request
        provider = _CountingProvider()
        with provider.patch(), pytest.raises(HTTPException) as exc:
            await _call_scan(factory, user_id)
        assert (exc.value.status_code, exc.value.detail) == (429, "scan_in_progress")
        assert exc.value.headers == {"Retry-After": "2"}
        assert provider.calls == 0
        assert await _units(factory, user_id, "day", quota.utc_today()) is None

    async def test_monthly_exhaustion_refuses_before_provider(
        self, sessions, downstream
    ):
        factory, make_user = sessions
        user_id = await make_user()
        today = quota.utc_today()
        month = quota.month_start(today)
        async with factory() as db:
            for _ in range(5):
                await quota.reserve_unit(db, user_id, FEATURE, "month", month, 5)
        provider = _CountingProvider()
        with provider.patch(), pytest.raises(scan_api.ScanLimitReachedError) as exc:
            await _call_scan(factory, user_id)
        assert exc.value.status_code == 402
        assert exc.value.body() == {
            "detail": "scan_limit_reached",
            "limit": 5,
            "used": 5,
            "period_end": quota.next_month_start(today).isoformat(),
        }
        assert provider.calls == 0
        assert await _units(factory, user_id, "day", today) == 0  # refunded
        assert await _lease(factory, user_id) is None

    @pytest.mark.parametrize(
        "error", [ScanUnavailableError("down"), __import__("httpx").ConnectError("x")]
    )
    async def test_provider_error_releases_reservations_and_lease(
        self, sessions, downstream, error
    ):
        factory, make_user = sessions
        user_id = await make_user()
        today = quota.utc_today()
        provider = _CountingProvider(delay=0, error=error)
        with provider.patch(), pytest.raises(HTTPException) as exc:
            await _call_scan(factory, user_id)
        assert (exc.value.status_code, exc.value.detail) == (503, "scan_unavailable")
        assert provider.calls == 1
        assert await _units(factory, user_id, "day", today) == 0
        assert await _units(factory, user_id, "month", quota.month_start(today)) == 0
        assert await _lease(factory, user_id) is None

    async def test_success_keeps_reservation_and_releases_lease(
        self, sessions, downstream
    ):
        factory, make_user = sessions
        user_id = await make_user()
        today = quota.utc_today()
        with _CountingProvider(delay=0).patch():
            await _call_scan(factory, user_id)
        assert await _units(factory, user_id, "day", today) == 1
        assert await _units(factory, user_id, "month", quota.month_start(today)) == 1
        assert await _lease(factory, user_id) is None

    async def test_unexpected_error_keeps_reservation_and_releases_lease(
        self, sessions, downstream
    ):
        factory, make_user = sessions
        user_id = await make_user()
        today = quota.utc_today()
        with (
            _CountingProvider(delay=0, error=RuntimeError("boom")).patch(),
            pytest.raises(RuntimeError),
        ):
            await _call_scan(factory, user_id)
        assert await _units(factory, user_id, "day", today) == 1
        assert await _lease(factory, user_id) is None

    async def test_failed_release_after_success_still_returns_the_scan(
        self, sessions, downstream
    ):
        """A lease release that fails rolls the request session back (which
        expires the loaded user); the rest of the request must not touch the
        expired user, and the credit is still spent."""
        factory, make_user = sessions
        user_id = await make_user()
        real_release = quota.release_lease
        calls = []

        async def flaky_release(db, uid, lease_id):
            calls.append(lease_id)
            if len(calls) == 1:
                # A real database error inside the session's transaction.
                await db.execute(select(func.sqrt(-1)))
            return await real_release(db, uid, lease_id)

        book = MagicMock()
        with (
            _CountingProvider(delay=0).patch(),
            patch.object(quota, "release_lease", flaky_release),
            patch("app.api.scan.EnrichmentService") as enrich,
            patch("app.api.scan.DeduplicationService") as dedup,
            patch("app.api.scan._dedupe_enriched", lambda books: books),
            patch("app.api.scan.ScanResponse", lambda **kw: kw),
        ):
            enrich.return_value.enrich = AsyncMock(return_value=[book])
            dedup.return_value.check = AsyncMock(return_value=[book])
            async with factory() as db:
                user = await db.get(User, user_id)
                credits_before = user.enhanced_scan_credits
                resp = await scan_api.scan.__wrapped__(
                    request=MagicMock(),
                    file=_upload(),
                    current_user=user,
                    db=db,
                    cf_turnstile_response=None,
                    enhanced=True,
                )
        assert len(calls) == 2  # retried after the rollback
        assert resp["enhanced_scan_credits"] == credits_before - 1
        assert dedup.return_value.check.await_args.args[1] == str(user_id)
        assert await _lease(factory, user_id) is None

    async def test_standard_and_enhanced_scans_share_one_allowance(
        self, sessions, downstream
    ):
        """OD-1: 3 standard + 2 enhanced scans use a daily limit of 5; the 6th
        scan of either kind is refused before the provider."""
        factory, make_user = sessions
        user_id = await make_user()
        today = quota.utc_today()
        provider = _CountingProvider(delay=0)
        with (
            provider.patch(),
            patch.object(settings, "scan_free_daily_limit", 5),
            patch.object(settings, "scan_free_monthly_limit", 100),
            patch.object(settings, "enhanced_scan_free_credits", 10),
        ):
            for enhanced in (False, False, False, True, True):
                await _call_scan(factory, user_id, enhanced=enhanced)
            for enhanced in (False, True):
                with pytest.raises(HTTPException) as exc:
                    await _call_scan(factory, user_id, enhanced=enhanced)
                assert (exc.value.status_code, exc.value.detail) == (
                    429,
                    "daily_scan_limit",
                )
        assert provider.calls == 5
        assert await _units(factory, user_id, "day", today) == 5
        assert await _units(factory, user_id, "month", quota.month_start(today)) == 5
        async with factory() as db:
            features = (
                await db.execute(
                    select(AIQuota.feature).where(AIQuota.user_id == user_id)
                )
            ).scalars()
            assert set(features) == {FEATURE}

    async def test_release_timeout_mid_query_leaves_session_usable(
        self, sessions, downstream
    ):
        """A lease release cut off by CLEANUP_TIMEOUT_SECONDS while its query is
        running must not leave the request session unusable: the dedup check
        (a real query on the same session) still runs and /scan returns."""
        factory, make_user = sessions
        user_id = await make_user()
        real_release = quota.release_lease
        seen: dict[str, bool] = {}

        async def slow_release(db, uid, lease_id):
            # Outlasts the 1 s bound, so the query is cancelled mid-execute.
            await db.execute(select(func.pg_sleep(3)))
            return await real_release(db, uid, lease_id)

        async def dedup_check(db, uid, books):
            # What DeduplicationService.check does: a query on the session.
            await db.execute(select(func.count()).select_from(User))
            seen["dedup_query_ran"] = True
            return books

        book = MagicMock()
        with (
            _CountingProvider(delay=0).patch(),
            patch.object(quota, "release_lease", slow_release),
            patch.object(quota, "CLEANUP_TIMEOUT_SECONDS", 1.0),
            patch("app.api.scan.EnrichmentService") as enrich,
            patch("app.api.scan.DeduplicationService") as dedup,
            patch("app.api.scan._dedupe_enriched", lambda books: books),
            patch("app.api.scan.ScanResponse", lambda **kw: kw),
        ):
            enrich.return_value.enrich = AsyncMock(return_value=[book])
            dedup.return_value.check = dedup_check
            async with factory() as db:
                user = await db.get(User, user_id)
                resp = await scan_api.scan.__wrapped__(
                    request=MagicMock(),
                    file=_upload(),
                    current_user=user,
                    db=db,
                    cf_turnstile_response=None,
                    enhanced=False,
                )
                # And the session still answers a plain query afterwards.
                assert (await db.execute(select(func.count()))).scalar_one() == 1
        assert resp["books"] == [book]
        assert seen == {"dedup_query_ran": True}

    async def test_usage_reads_current_windows(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        today = quota.utc_today()
        async with factory() as db:
            await quota.reserve_unit(db, user_id, FEATURE, "day", today, 5)
            for _ in range(3):
                await quota.reserve_unit(
                    db, user_id, FEATURE, "month", quota.month_start(today), 5
                )
            # Other feature and an older day: not counted.
            await quota.reserve_unit(db, user_id, OTHER_FEATURE, "day", today, 5)
            await quota.reserve_unit(
                db, user_id, FEATURE, "day", today - timedelta(days=1), 5
            )
            user = await db.get(User, user_id)
            usage = await quota.usage_for(
                db, user=user, feature=FEATURE, ai=AISettings({})
            )
        assert (usage.used_today, usage.used_month) == (1, 3)
        assert (usage.daily_limit, usage.monthly_limit) == (5, 5)
        assert usage.period_end == quota.next_month_start(today)


class TestPurge:
    async def test_purge_deletes_only_old_rows_in_batches(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        today = date(2026, 10, 21)
        day_cutoff, month_cutoff = quota.purge_cutoffs(today)
        assert month_cutoff == date(2025, 9, 1)
        old_days = [day_cutoff - timedelta(days=n) for n in range(1, 6)]
        kept_days = [day_cutoff, today]
        old_months = [date(2025, 8, 1), date(2025, 7, 1), date(2024, 12, 1)]
        kept_months = [month_cutoff, date(2026, 10, 1)]
        async with factory() as db:
            for d in old_days + kept_days:
                await quota.reserve_unit(db, user_id, FEATURE, "day", d, 10)
            for m in old_months + kept_months:
                await quota.reserve_unit(db, user_id, FEATURE, "month", m, 10)
            with patch.object(quota, "PURGE_BATCH_SIZE", 2):
                deleted = await quota.purge_old_quota(db, today=today)
        # Other tests' rows (far in the past) may be purged too.
        assert deleted >= len(old_days) + len(old_months)
        for d in old_days:
            assert await _units(factory, user_id, "day", d) is None
        for d in kept_days:
            assert await _units(factory, user_id, "day", d) == 1
        for m in old_months:
            assert await _units(factory, user_id, "month", m) is None
        for m in kept_months:
            assert await _units(factory, user_id, "month", m) == 1

    async def test_purge_stops_at_max_batches(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        today = date(2026, 10, 21)
        day_cutoff, _ = quota.purge_cutoffs(today)
        async with factory() as db:
            for n in range(1, 6):
                await quota.reserve_unit(
                    db, user_id, FEATURE, "day", day_cutoff - timedelta(days=n), 10
                )
            with (
                patch.object(quota, "PURGE_BATCH_SIZE", 1),
                patch.object(quota, "PURGE_MAX_BATCHES", 2),
            ):
                await quota.purge_old_quota(db, today=today)
        remaining = [
            await _units(factory, user_id, "day", day_cutoff - timedelta(days=n))
            for n in range(1, 6)
        ]
        # Other tests' old rows may take some of the two batches; at most 2
        # of this user's 5 rows are deleted in one run.
        assert remaining.count(None) <= 2
        assert remaining.count(1) >= 3


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0007", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_cycle(connection) -> None:
    migration = _load_migration()
    assert (migration.revision, migration.down_revision) == ("0007", "0006")
    context = MigrationContext.configure(connection)
    tables = ("ai_quota", "ai_inflight")

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
        migration.downgrade()
        assert present() == set()
        migration.upgrade()
        assert diffs() == []


async def test_migration_upgrade_check_downgrade_cycle():
    """Runs 0007 in a transaction that is rolled back (Postgres DDL is
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

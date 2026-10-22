"""Enhanced scans on real Postgres (#533): the Premium enhanced allowance under
concurrency, the zero-result charge decision in the settle transaction, the
threshold actions, the sliding window, the warning, the 30-day purge, the
cascade on account deletion and the 0010 migration cycle.

Needs DATABASE_URL pointing at a Postgres database.
"""

import asyncio
import importlib.util
import io
import uuid
from datetime import timedelta
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

from app.ai import entitlements, quota
from app.ai import settings as ai_settings
from app.ai.entitlements import Tier
from app.ai.providers.base import ProviderResult
from app.api import scan as scan_api
from app.core.config import settings
from app.core.database import Base
from app.models.ai_enhanced_zero_result import AIEnhancedZeroResult
from app.models.ai_quota import AIInflight, AIQuota
from app.models.ai_usage import AIUsage
from app.models.user import User
from app.schemas.book import EnrichedBook
from app.services.book_identifier import BookCandidate, ScanUnavailableError

pytestmark = pytest.mark.integration

ENHANCED = quota.ENHANCED_QUOTA_FEATURE
MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0010_ai_enhanced_zero_results.py"
)
DUNE = BookCandidate(title="Dune", author="Frank Herbert", confidence=0.9)


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
        await db.execute(delete(AIUsage).where(AIUsage.user_id.in_(created)))
        await db.execute(delete(User).where(User.id.in_(created)))
        await db.commit()
    await engine.dispose()


@pytest.fixture
def premium():
    with patch.object(entitlements, "tier_for", AsyncMock(return_value=Tier.PREMIUM)):
        yield


def _settings(**rows):
    ai_settings.reset_cache(
        {
            "quota.premium.daily": 1000,
            "quota.premium.monthly": 100_000,
            "quota.premium.enhanced_monthly": 20,
            "scan.enhanced.zero_result_threshold": 3,
            "scan.enhanced.zero_result_window_hours": 24,
            "scan.enhanced.zero_result_action": "charge",
            **rows,
        }
    )


class _Provider:
    def __init__(self, books=False, delay=0.0, error=None):
        self.books = books
        self.delay = delay
        self.error = error
        self.calls = 0

    async def identify(self, _image):
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return ProviderResult([DUNE] if self.books else [])

    def patch(self):
        cls = MagicMock()
        cls.return_value.identify = self.identify
        return patch("app.ai.gateway.OpenAIVisionProvider", cls)


@pytest.fixture
def downstream():
    """Enrichment returns one book per candidate; dedup passes them through."""

    async def enrich(candidates, limit):
        return [
            EnrichedBook(title=c.title, author=c.author, confidence=c.confidence)
            for c in candidates
        ]

    with (
        patch("app.api.scan.EnrichmentService") as enrich_cls,
        patch("app.api.scan.DeduplicationService") as dedup_cls,
        patch.object(settings, "turnstile_required", False),
    ):
        enrich_cls.return_value.enrich = AsyncMock(side_effect=enrich)
        dedup_cls.return_value.check = AsyncMock(side_effect=lambda _d, _u, b: b)
        yield


def _upload() -> StarletteUploadFile:
    return StarletteUploadFile(
        file=io.BytesIO(_JPEG),
        filename="scan.jpg",
        headers=Headers({"content-type": "image/jpeg"}),
    )


async def _scan(factory, user_id, enhanced=True):
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


async def _enhanced_used(factory, user_id) -> int:
    async with factory() as db:
        return (
            await db.execute(
                select(AIQuota.units_used).where(
                    AIQuota.user_id == user_id,
                    AIQuota.feature == ENHANCED,
                    AIQuota.window_kind == "month",
                    AIQuota.window_start == quota.month_start(quota.utc_today()),
                )
            )
        ).scalar_one_or_none() or 0


async def _zero_rows(factory, user_id) -> list[AIEnhancedZeroResult]:
    async with factory() as db:
        return list(
            (
                await db.execute(
                    select(AIEnhancedZeroResult)
                    .where(AIEnhancedZeroResult.user_id == user_id)
                    .order_by(AIEnhancedZeroResult.created_at)
                )
            ).scalars()
        )


async def _age_rows(factory, user_id, by: timedelta) -> None:
    async with factory() as db:
        await db.execute(
            update(AIEnhancedZeroResult)
            .where(AIEnhancedZeroResult.user_id == user_id)
            .values(created_at=AIEnhancedZeroResult.created_at - by)
        )
        await db.commit()


async def _insert_rows(factory, user_id, ages: list[timedelta]) -> None:
    async with factory() as db:
        for age in ages:
            db.add(
                AIEnhancedZeroResult(user_id=user_id, created_at=quota.utc_now() - age)
            )
        await db.commit()


async def _expire_lease(factory, user_id) -> None:
    async with factory() as db:
        await db.execute(
            update(AIInflight)
            .where(AIInflight.user_id == user_id)
            .values(lease_expires_at=quota._clock_timestamp() - timedelta(seconds=1))
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Allowance under concurrency
# ---------------------------------------------------------------------------


class TestConcurrency:
    async def test_concurrent_enhanced_scans_at_quota_exactly_n_provider_calls(
        self, sessions, downstream, premium
    ):
        factory, make_user = sessions
        users = [await make_user() for _ in range(3)]
        _settings(**{"quota.premium.enhanced_monthly": 3})
        provider = _Provider(books=True, delay=0.02)
        refusals: list[str] = []

        async def one(uid):
            try:
                await _scan(factory, uid)
            except HTTPException as exc:
                refusals.append(exc.detail)

        with provider.patch():
            for _ in range(10):
                await asyncio.gather(*(one(uid) for uid in users for _ in range(4)))
        assert provider.calls == 3 * len(users)
        for uid in users:
            assert await _enhanced_used(factory, uid) == 3
            async with factory() as db:
                assert await db.get(AIInflight, uid) is None
        assert set(refusals) <= {"enhanced_limit_reached", "scan_in_progress"}
        assert "enhanced_limit_reached" in refusals


# ---------------------------------------------------------------------------
# The charge decision
# ---------------------------------------------------------------------------


class TestZeroResult:
    async def test_zero_result_is_uncharged_and_writes_one_row_with_the_ledger_id(
        self, sessions, downstream, premium
    ):
        factory, make_user = sessions
        user_id = await make_user()
        _settings()
        with _Provider(books=False).patch():
            resp = await _scan(factory, user_id)
        assert resp.books == []
        assert resp.enhanced_scans_remaining == 20
        assert await _enhanced_used(factory, user_id) == 0
        (row,) = await _zero_rows(factory, user_id)
        async with factory() as db:
            (usage,) = (
                await db.execute(select(AIUsage).where(AIUsage.user_id == user_id))
            ).scalars()
        assert row.ledger_id == usage.call_id

    async def test_books_found_is_charged_and_writes_no_row(
        self, sessions, downstream, premium
    ):
        factory, make_user = sessions
        user_id = await make_user()
        _settings()
        with _Provider(books=True).patch():
            resp = await _scan(factory, user_id)
        assert resp.enhanced_scans_remaining == 19
        assert await _enhanced_used(factory, user_id) == 1
        assert await _zero_rows(factory, user_id) == []

    async def test_row_is_committed_with_the_settle(
        self, sessions, downstream, premium
    ):
        """Just before the lease delete commits, the zero-result row and the
        refunded unit are visible on the request session but not on another
        connection: one COMMIT writes them with the usage row and the
        release."""
        factory, make_user = sessions
        user_id = await make_user()
        _settings()
        real_release = quota.release_lease
        facts: dict[str, object] = {}

        async def spy(db, uid, lease_id):
            own = await db.execute(
                select(func.count())
                .select_from(AIEnhancedZeroResult)
                .where(AIEnhancedZeroResult.user_id == uid)
            )
            facts["own"] = own.scalar_one()
            facts["other"] = len(await _zero_rows(factory, uid))
            facts["other_used"] = await _enhanced_used(factory, uid)
            return await real_release(db, uid, lease_id)

        with (
            _Provider(books=False).patch(),
            patch.object(quota, "release_lease", spy),
        ):
            await _scan(factory, user_id)
        assert facts == {"own": 1, "other": 0, "other_used": 1}
        assert len(await _zero_rows(factory, user_id)) == 1
        assert await _enhanced_used(factory, user_id) == 0

    async def test_failed_settle_rolls_back_and_retry_writes_one_row(
        self, sessions, downstream, premium
    ):
        factory, make_user = sessions
        user_id = await make_user()
        _settings()
        real_release = quota.release_lease
        attempts = []

        async def fail_once(db, uid, lease_id):
            attempts.append(lease_id)
            if len(attempts) == 1:
                await db.execute(select(func.sqrt(-1)))  # fails before commit
            return await real_release(db, uid, lease_id)

        with (
            _Provider(books=False).patch(),
            patch.object(quota, "release_lease", fail_once),
        ):
            await _scan(factory, user_id)
        assert len(attempts) == 2
        assert len(await _zero_rows(factory, user_id)) == 1
        assert await _enhanced_used(factory, user_id) == 0

    async def test_retried_settle_after_lost_ack_writes_no_second_row(
        self, sessions, downstream, premium
    ):
        """The first settle commits, then its acknowledgement is lost (an
        error after the commit). The retry finds the usage row already there
        and applies nothing again: one row, one refund."""
        factory, make_user = sessions
        user_id = await make_user()
        _settings()
        # Another unit already used this month, so a double refund would show.
        async with factory() as db:
            await quota.reserve_unit(
                db,
                user_id,
                ENHANCED,
                "month",
                quota.month_start(quota.utc_today()),
                20,
            )
        real_release = quota.release_lease
        attempts = []

        async def ack_lost(db, uid, lease_id):
            attempts.append(lease_id)
            done = await real_release(db, uid, lease_id)  # commits
            if len(attempts) == 1:
                raise ConnectionError("acknowledgement lost")
            return done

        with (
            _Provider(books=False).patch(),
            patch.object(quota, "release_lease", ack_lost),
        ):
            await _scan(factory, user_id)
        assert len(attempts) == 2
        assert len(await _zero_rows(factory, user_id)) == 1
        assert await _enhanced_used(factory, user_id) == 1  # refunded once only

    @pytest.mark.parametrize(
        "error", [ScanUnavailableError("down"), RuntimeError("provider blew up")]
    )
    async def test_provider_error_writes_no_row(
        self, sessions, downstream, premium, error
    ):
        factory, make_user = sessions
        user_id = await make_user()
        _settings()
        with (
            _Provider(error=error).patch(),
            pytest.raises((HTTPException, RuntimeError)),
        ):
            await _scan(factory, user_id)
        assert await _zero_rows(factory, user_id) == []
        # Unavailable refunds the unit with the shared ones; another error
        # keeps it (the provider may have been paid).
        expected = 0 if isinstance(error, ScanUnavailableError) else 1
        assert await _enhanced_used(factory, user_id) == expected


class TestThreshold:
    async def test_charge_up_to_threshold_free_then_charged(
        self, sessions, downstream, premium
    ):
        factory, make_user = sessions
        user_id = await make_user()
        _settings(**{"scan.enhanced.zero_result_threshold": 3})
        with _Provider(books=False).patch():
            for _ in range(3):
                await _scan(factory, user_id)
            assert await _enhanced_used(factory, user_id) == 0
            assert len(await _zero_rows(factory, user_id)) == 3
            resp = await _scan(factory, user_id)
        assert await _enhanced_used(factory, user_id) == 1
        assert len(await _zero_rows(factory, user_id)) == 3
        assert resp.enhanced_scans_remaining == 19

    async def test_throttle_refuses_the_next_enhanced_before_the_provider(
        self, sessions, downstream, premium
    ):
        factory, make_user = sessions
        user_id = await make_user()
        _settings(
            **{
                "scan.enhanced.zero_result_threshold": 2,
                "scan.enhanced.zero_result_action": "throttle",
                "scan.enhanced.zero_result_window_hours": 2,
            }
        )
        provider = _Provider(books=False)
        with provider.patch():
            await _scan(factory, user_id)
            await _scan(factory, user_id)
            async with factory() as db:
                units_before = list(
                    (
                        await db.execute(
                            select(AIQuota.feature, AIQuota.units_used).where(
                                AIQuota.user_id == user_id
                            )
                        )
                    ).all()
                )
            with pytest.raises(HTTPException) as exc:
                await _scan(factory, user_id)
            # Standard scans are not affected.
            await _scan(factory, user_id, enhanced=False)
        assert (exc.value.status_code, exc.value.detail) == (429, "enhanced_throttled")
        retry_after = int(exc.value.headers["Retry-After"])
        assert 2 * 3600 - 120 <= retry_after <= 2 * 3600
        assert provider.calls == 3  # two enhanced + the standard one
        async with factory() as db:
            assert await db.get(AIInflight, user_id) is None
            units_after = dict(
                (
                    await db.execute(
                        select(AIQuota.feature, AIQuota.units_used).where(
                            AIQuota.user_id == user_id,
                            AIQuota.window_kind == "month",
                        )
                    )
                ).all()
            )
        assert units_after[ENHANCED] == dict(units_before)[ENHANCED]

    async def test_window_expiry_makes_zero_results_free_again(
        self, sessions, downstream, premium
    ):
        factory, make_user = sessions
        user_id = await make_user()
        _settings(
            **{
                "scan.enhanced.zero_result_threshold": 2,
                "scan.enhanced.zero_result_window_hours": 1,
            }
        )
        with _Provider(books=False).patch():
            await _scan(factory, user_id)
            await _scan(factory, user_id)
            await _scan(factory, user_id)  # charged
            assert await _enhanced_used(factory, user_id) == 1
            await _age_rows(factory, user_id, timedelta(hours=1, seconds=1))
            await _scan(factory, user_id)  # free again
        assert await _enhanced_used(factory, user_id) == 1
        assert len(await _zero_rows(factory, user_id)) == 3

    async def test_window_edge(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        await _insert_rows(
            factory,
            user_id,
            [
                timedelta(hours=24) - timedelta(seconds=30),  # just inside
                timedelta(hours=24) + timedelta(seconds=30),  # just outside
            ],
        )
        async with factory() as db:
            assert await quota.zero_result_count(db, user_id, 24) == 1
            assert await quota.warned_in_window(db, user_id, 24) is False
            # Retry-After: the inside row leaves in about 30 s.
            assert 1 <= await quota.zero_result_throttle(db, user_id, 1, 24) <= 31
            assert await quota.zero_result_throttle(db, user_id, 2, 24) is None

    @pytest.mark.parametrize("threshold,leaves_in_hours", [(1, 23), (2, 22), (3, 21)])
    async def test_retry_after_when_count_is_above_threshold(
        self, sessions, threshold, leaves_in_hours
    ):
        """E.g. the threshold was lowered from 3 to 1 with 3 rows in the
        window: the count drops below the threshold only once the newest of
        the rows that must go (the (count - threshold + 1)-th oldest) has left
        the window."""
        factory, make_user = sessions
        user_id = await make_user()
        await _insert_rows(
            factory,
            user_id,
            [timedelta(hours=3), timedelta(hours=2), timedelta(hours=1)],
        )
        async with factory() as db:
            retry_after = await quota.zero_result_throttle(db, user_id, threshold, 24)
        expected = leaves_in_hours * 3600
        assert expected - 60 <= retry_after <= expected + 1

    async def test_users_do_not_share_counts(self, sessions, downstream, premium):
        factory, make_user = sessions
        noisy, quiet = await make_user(), await make_user()
        await _insert_rows(factory, noisy, [timedelta(minutes=1)] * 5)
        _settings(**{"scan.enhanced.zero_result_threshold": 1})
        with _Provider(books=False).patch():
            await _scan(factory, quiet)
        assert await _enhanced_used(factory, quiet) == 0  # free: own count 0

    async def test_warning_once_per_user_per_window(
        self, sessions, downstream, premium
    ):
        factory, make_user = sessions
        user_id = await make_user()
        _settings(**{"scan.enhanced.zero_result_threshold": 2})
        with (
            _Provider(books=False).patch(),
            patch.object(quota, "send_zero_result_warning") as warn,
        ):
            for _ in range(5):
                await _scan(factory, user_id)
            assert warn.call_count == 1
            assert warn.call_args.kwargs == {
                "count": 2,
                "threshold": 2,
                "window_hours": 24,
                "action": "charge",
            }
            # A new window: the old rows have left it.
            await _age_rows(factory, user_id, timedelta(hours=25))
            for _ in range(3):
                await _scan(factory, user_id)
        assert warn.call_count == 2

    async def test_sliding_window_abuser_gets_one_warning_per_window(
        self, sessions, downstream, premium
    ):
        """Threshold 3. Rows age out one by one, so the count keeps returning
        to the threshold; only one warning is sent while the row that sent it
        is inside the window, and a new one once it has left."""
        factory, make_user = sessions
        user_id = await make_user()
        _settings(**{"scan.enhanced.zero_result_threshold": 3})

        async def age(row_id, hours=25):
            async with factory() as db:
                await db.execute(
                    update(AIEnhancedZeroResult)
                    .where(AIEnhancedZeroResult.id == row_id)
                    .values(created_at=quota.utc_now() - timedelta(hours=hours))
                )
                await db.commit()

        with (
            _Provider(books=False).patch(),
            patch.object(quota, "send_zero_result_warning") as warn,
        ):
            for _ in range(3):
                await _scan(factory, user_id)
            assert warn.call_count == 1
            r1, r2, r3 = await _zero_rows(factory, user_id)
            assert [r.warned for r in (r1, r2, r3)] == [False, False, True]

            await age(r1.id)  # count back to 2
            await _scan(factory, user_id)  # count 3 again: free, no warning
            await age(r2.id)
            await _scan(factory, user_id)  # count 3 again: free, no warning
            assert warn.call_count == 1
            assert await _enhanced_used(factory, user_id) == 0

            await age(r3.id)  # the warned row leaves the window
            await _scan(factory, user_id)
            assert warn.call_count == 2
        rows = await _zero_rows(factory, user_id)
        assert sum(r.warned for r in rows) == 2


class TestTwoSettles:
    """Two settles of the same user interleaved: a request whose lease expired
    and was taken over settles while the new holder settles. Only the holder
    of the lease may record (the lease row is locked FOR UPDATE until its
    commit), so at most `threshold` rows are uncharged and at most one warning
    is claimed, in the same month and across a UTC month boundary."""

    @pytest.mark.parametrize("cross_month", [False, True])
    @pytest.mark.parametrize("stale_first", [True, False])
    async def test_at_most_threshold_uncharged_and_one_warning(
        self, sessions, cross_month, stale_first
    ):
        factory, make_user = sessions
        user_id = await make_user()
        today = quota.utc_today()
        month = quota.month_start(today)
        other_month = quota.next_month_start(today) if cross_month else month
        async with factory() as db:
            stale_lease = await quota.acquire_lease(db, user_id, 60)
            for m in {month, other_month}:
                await quota.reserve_unit(db, user_id, ENHANCED, "month", m, 20)
                await quota.reserve_unit(db, user_id, ENHANCED, "month", m, 20)
        await _expire_lease(factory, user_id)
        async with factory() as db:
            holder_lease = await quota.acquire_lease(db, user_id, 60)
        assert holder_lease is not None and holder_lease != stale_lease
        await _insert_rows(factory, user_id, [timedelta(hours=2), timedelta(hours=1)])

        def reservation(lease_id, m):
            return quota.Reservation(
                user_id=user_id,
                feature=quota.SCAN_QUOTA_FEATURE,
                lease_id=lease_id,
                day=today,
                month=m,
                tier=Tier.PREMIUM,
                allowance=quota.Allowance(30, 300),
                enhanced=True,
                enhanced_allowance=20,
                zero_result_threshold=3,
                zero_result_window_hours=24,
            )

        stale = reservation(stale_lease, month)
        holder = reservation(holder_lease, other_month)
        first, second = (stale, holder) if stale_first else (holder, stale)
        async with factory() as db1, factory() as db2:
            r1 = await quota.record_zero_result(db1, first, None)
            task = asyncio.create_task(quota.record_zero_result(db2, second, None))
            await asyncio.sleep(0.3)
            await db1.commit()
            r2 = await task
            await db2.commit()
        results = {id(first): r1, id(second): r2}
        assert results[id(stale)] is None  # charged: no longer its lease
        assert results[id(holder)] == quota.ZeroResultRecorded(3, warn=True)
        rows = await _zero_rows(factory, user_id)
        assert len(rows) == 3  # threshold, not threshold + 1
        assert sum(r.warned for r in rows) == 1
        async with factory() as db:
            used = dict(
                (
                    await db.execute(
                        select(AIQuota.window_start, AIQuota.units_used).where(
                            AIQuota.user_id == user_id, AIQuota.feature == ENHANCED
                        )
                    )
                ).all()
            )
        # Only the holder's unit was given back.
        if cross_month:
            assert used == {month: 2, other_month: 1}
        else:
            assert used == {month: 1}


class TestEnhancedRefundOnce:
    """A give-back of the enhanced unit that commits and then loses its
    acknowledgement is not retried (a retry would take a second unit off)."""

    @pytest.fixture
    def ack_lost(self):
        real = quota.refund_unit
        enhanced_calls: list = []

        async def refund(db, user_id, feature, kind, start):
            await real(db, user_id, feature, kind, start)  # commits
            if feature == ENHANCED:
                enhanced_calls.append(start)
                raise ConnectionError("acknowledgement lost")

        with patch.object(quota, "refund_unit", refund):
            yield enhanced_calls

    async def _one_unit_used(self, factory, user_id):
        async with factory() as db:
            await quota.reserve_unit(
                db, user_id, ENHANCED, "month", quota.month_start(quota.utc_today()), 20
            )

    async def test_provider_unavailable_refund(
        self, sessions, downstream, premium, ack_lost
    ):
        factory, make_user = sessions
        user_id = await make_user()
        _settings()
        await self._one_unit_used(factory, user_id)
        with (
            _Provider(error=ScanUnavailableError("down")).patch(),
            pytest.raises(HTTPException),
        ):
            await _scan(factory, user_id)
        assert len(ack_lost) == 1
        assert await _enhanced_used(factory, user_id) == 1

    async def test_reserve_give_back_after_lost_lease(
        self, sessions, downstream, premium, ack_lost
    ):
        factory, make_user = sessions
        user_id = await make_user()
        _settings()
        await self._one_unit_used(factory, user_id)
        provider = _Provider(books=True)
        with (
            provider.patch(),
            patch.object(quota, "renew_lease", AsyncMock(return_value=False)),
            pytest.raises(HTTPException) as exc,
        ):
            await _scan(factory, user_id)
        assert exc.value.detail == "scan_in_progress"
        assert provider.calls == 0
        assert len(ack_lost) == 1
        assert await _enhanced_used(factory, user_id) == 1


# ---------------------------------------------------------------------------
# Retention and account deletion
# ---------------------------------------------------------------------------


class TestRetention:
    async def test_purge_removes_rows_older_than_30_days_in_batches(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        await _insert_rows(
            factory,
            user_id,
            [timedelta(days=31)] * 5 + [timedelta(days=29), timedelta(hours=1)],
        )
        with patch.object(quota, "PURGE_BATCH_SIZE", 2):
            async with factory() as db:
                deleted = await quota.purge_old_zero_results(db)
        assert deleted >= 5
        rows = await _zero_rows(factory, user_id)
        assert len(rows) == 2

    async def test_purge_is_bounded_per_run(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        await _insert_rows(factory, user_id, [timedelta(days=40)] * 5)
        with (
            patch.object(quota, "PURGE_BATCH_SIZE", 1),
            patch.object(quota, "PURGE_MAX_BATCHES", 2),
        ):
            async with factory() as db:
                await quota.purge_old_zero_results(db)
        # Other tests' old rows may be purged first; at most 2 deleted here.
        assert len(await _zero_rows(factory, user_id)) >= 3

    async def test_deleting_a_user_cascades(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        await _insert_rows(factory, user_id, [timedelta(minutes=1)] * 2)
        async with factory() as db:
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()
        assert await _zero_rows(factory, user_id) == []


# ---------------------------------------------------------------------------
# Migration 0010
# ---------------------------------------------------------------------------


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0010", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_cycle(connection) -> None:
    migration = _load_migration()
    assert (migration.revision, migration.down_revision) == ("0010", "0009")
    context = MigrationContext.configure(connection)
    table = "ai_enhanced_zero_results"

    def present() -> bool:
        return inspect(connection).has_table(table)

    def diffs() -> list:
        return [d for d in compare_metadata(context, Base.metadata) if table in repr(d)]

    with Operations.context(context):
        if present():
            migration.downgrade()
        assert not present()
        migration.upgrade()
        assert present()
        assert diffs() == []
        indexes = {
            i["name"]: i["column_names"] for i in inspect(connection).get_indexes(table)
        }
        assert indexes["ix_ai_enhanced_zero_results_user_created"] == [
            "user_id",
            "created_at",
        ]
        columns = {c["name"]: c for c in inspect(connection).get_columns(table)}
        assert columns["warned"]["nullable"] is False
        assert "false" in str(columns["warned"]["default"]).lower()
        (fk,) = inspect(connection).get_foreign_keys(table)
        assert fk["referred_table"] == "users"
        assert fk["options"].get("ondelete") == "CASCADE"
        migration.downgrade()
        assert not present()
        migration.upgrade()
        assert diffs() == []


async def test_migration_upgrade_check_downgrade_cycle():
    """Runs 0010 in a transaction that is rolled back (Postgres DDL is
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

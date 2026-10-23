"""The daily cost circuit breaker (#531): threshold math, the alert mask, the
SQL of the conditional reserve, quota.reserve / quota.release wiring and the
/scan mapping. The DB primitives are faked in memory (``FakeQuotaStore``);
tests/integration/test_ai_breaker_db.py runs them against Postgres."""

import asyncio
import io
import json
import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import httpx
import pytest
import sentry_sdk
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.dialects import postgresql

from app.ai import breaker, entitlements, ledger, quota
from app.ai import settings as ai_settings
from app.ai.entitlements import Tier
from app.ai.providers.base import ProviderResult
from app.ai.registry import get_feature
from app.ai.settings import AISettings
from app.main import app
from app.models.user import User
from app.services.book_identifier import ScanUnavailableError
from tests.unit._quota_fake import FakeQuotaStore
from tests.unit._sentry_capture import captured_events

USER = User(
    id=uuid.UUID("00000000-0000-0000-0000-000000000531"),
    email="breaker@example.com",
    enhanced_scan_credits=3,
)
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
TODAY = date(2026, 10, 21)
EST = 10_000
CEILING = 1_000_000


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


@pytest.fixture
def store():
    s = FakeQuotaStore(default_user=USER.id)
    with s.installed():
        yield s


def _ai(**rows) -> AISettings:
    return AISettings({"breaker.free_daily_ceiling_micros": CEILING, **rows})


async def _reserve(ai=None, est=EST):
    return await quota.reserve(
        AsyncMock(),
        user=USER,
        feature=quota.SCAN_QUOTA_FEATURE,
        timeout_s=60,
        ai=ai or _ai(),
        est_cost_micros=est,
        now=NOW,
    )


def _premium(monkeypatch):
    monkeypatch.setattr(entitlements, "tier_for", AsyncMock(return_value=Tier.PREMIUM))


# ---------------------------------------------------------------------------
# Threshold math and the alert mask
# ---------------------------------------------------------------------------


class TestThresholds:
    @pytest.mark.parametrize(
        ("settled", "crossed"),
        [
            (0, ()),
            (499_000, ()),  # 49.9 %
            (499_999, ()),
            (500_000, (50,)),  # 50 %
            (749_999, (50,)),
            (750_000, (50, 75)),  # 75 %
            (899_999, (50, 75)),
            (900_000, (50, 75, 90)),  # 90 %
            (1_000_000, (50, 75, 90)),  # 100 %
            (5_000_000, (50, 75, 90)),
        ],
    )
    def test_crossed_thresholds(self, settled, crossed):
        assert breaker.crossed_thresholds(settled, CEILING) == crossed

    def test_integer_math_has_no_rounding_gap(self):
        # 50 % of an odd ceiling: 3 * 100 >= 50 * 5 but 2 * 100 < 50 * 5.
        assert breaker.crossed_thresholds(3, 5) == (50,)
        assert breaker.crossed_thresholds(2, 5) == ()

    def test_zero_ceiling_sends_no_alerts(self):
        assert breaker.crossed_thresholds(10**9, 0) == ()

    def test_bits_are_distinct(self):
        bits = [breaker.threshold_bit(p) for p in breaker.ALERT_THRESHOLDS_PCT]
        assert bits == [1, 2, 4]

    def test_mask_prevents_a_second_alert_for_the_same_threshold(self):
        assert breaker.pending_alerts(800_000, CEILING, 0) == (50, 75)
        assert breaker.pending_alerts(800_000, CEILING, 0b001) == (75,)
        assert breaker.pending_alerts(800_000, CEILING, 0b011) == ()
        assert breaker.pending_alerts(950_000, CEILING, 0b011) == (90,)
        assert breaker.pending_alerts(950_000, CEILING, 0b111) == ()


# ---------------------------------------------------------------------------
# The SQL each primitive sends
# ---------------------------------------------------------------------------


class _CaptureDB:
    def __init__(self, scalar=None, row=None):
        self.stmts = []
        self.commits = 0
        self._scalar = scalar
        self._row = row

    async def execute(self, stmt):
        self.stmts.append(stmt)
        result = MagicMock()
        result.scalar_one_or_none.return_value = self._scalar
        result.one_or_none.return_value = self._row
        return result

    async def commit(self):
        self.commits += 1


class TestSQL:
    async def test_free_reserve_is_a_conditional_upsert(self):
        db = _CaptureDB(scalar=TODAY)
        assert await breaker.reserve(db, TODAY, EST, CEILING, enforce=True)
        (stmt,) = db.stmts
        sql = _sql(stmt)
        assert "INSERT INTO ai_spend_daily" in sql
        assert "ON CONFLICT (day) DO UPDATE" in sql
        assert (
            "WHERE ai_spend_daily.reserved_micros + ai_spend_daily.settled_micros"
            in sql
        )
        assert "<=" in sql
        assert "RETURNING ai_spend_daily.day" in sql
        assert db.commits == 1

    async def test_free_refusal_returns_false(self):
        db = _CaptureDB(scalar=None)
        assert not await breaker.reserve(db, TODAY, EST, CEILING, enforce=True)
        assert db.commits == 1

    async def test_premium_reserve_has_no_ceiling_condition(self):
        db = _CaptureDB(scalar=TODAY)
        assert await breaker.reserve(db, TODAY, EST, 0, enforce=False)
        sql = _sql(db.stmts[0])
        assert "ON CONFLICT (day) DO UPDATE" in sql
        assert "WHERE" not in sql.split("DO UPDATE")[1]

    async def test_free_estimate_above_the_ceiling_refuses_without_writing(self):
        """The insert path (first call of the day) has no WHERE, so an
        estimate larger than the whole ceiling must be refused up front."""
        db = _CaptureDB(scalar=TODAY)
        assert not await breaker.reserve(db, TODAY, EST, EST - 1, enforce=True)
        assert db.stmts == []

    async def test_settle_moves_estimate_and_adds_cost_without_commit(self):
        db = _CaptureDB(row=(600, 0, 0))
        assert await breaker.settle(db, TODAY, EST, 600) == (600, 0, 0)
        sql = _sql(db.stmts[0])
        assert "UPDATE ai_spend_daily SET" in sql
        assert "greatest(ai_spend_daily.reserved_micros -" in sql
        assert "settled_micros=(ai_spend_daily.settled_micros +" in sql
        assert db.commits == 0  # committed by the lease release

    async def test_release_never_goes_below_zero_and_commits(self):
        db = _CaptureDB()
        await breaker.release(db, TODAY, EST)
        assert "greatest(" in _sql(db.stmts[0])
        assert db.commits == 1

    async def test_claim_is_conditional_on_the_bit(self):
        db = _CaptureDB(scalar=TODAY)
        assert await breaker.claim_alerts(db, TODAY, (50, 75)) == [50, 75]
        sql = _sql(db.stmts[1])
        where = sql.split(" WHERE ")[1]
        assert "(ai_spend_daily.alerted_mask & " in where
        assert where.split("RETURNING")[0].rstrip().endswith("= %(param_1)s::SMALLINT")
        assert "SET alerted_mask=(ai_spend_daily.alerted_mask | " in sql
        assert db.commits == 1

    async def test_claim_lost_to_another_request_returns_nothing(self):
        db = _CaptureDB(scalar=None)
        assert await breaker.claim_alerts(db, TODAY, (50,)) == []

    @pytest.mark.parametrize("bad", [-1, True, 1.5, None])
    async def test_amounts_must_be_non_negative_ints(self, bad):
        with pytest.raises(ValueError):
            await breaker.reserve(_CaptureDB(), TODAY, bad, CEILING, enforce=True)
        with pytest.raises(ValueError):
            await breaker.settle(_CaptureDB(), TODAY, EST, bad)


# ---------------------------------------------------------------------------
# quota.reserve: the cost ceiling in the check order
# ---------------------------------------------------------------------------


class TestReserve:
    async def test_free_reservation_adds_the_estimate(self, store):
        r = await _reserve()
        assert store.spend[TODAY][0] == EST
        assert (r.est_cost_micros, r.ceiling_micros) == (EST, CEILING)

    async def test_free_refusal_at_the_ceiling_takes_nothing_per_user(self, store):
        store.spend[TODAY] = [0, CEILING - EST + 1, 0]
        with pytest.raises(quota.CostCeilingReached):
            await _reserve()
        assert store.leases == {} and store.rows == {}
        assert store.spend[TODAY] == [0, CEILING - EST + 1, 0]
        assert store.cost_refusals == 1

    async def test_free_reservation_exactly_at_the_ceiling_succeeds(self, store):
        store.spend[TODAY] = [EST, CEILING - 2 * EST, 0]
        await _reserve()
        assert store.spend[TODAY][0] + store.spend[TODAY][1] == CEILING

    async def test_zero_ceiling_pauses_free(self, store):
        with pytest.raises(quota.CostCeilingReached):
            await _reserve(_ai(**{"breaker.free_daily_ceiling_micros": 0}))

    async def test_premium_at_the_ceiling_still_reserves_and_adds(
        self, store, monkeypatch
    ):
        _premium(monkeypatch)
        store.spend[TODAY] = [0, CEILING, 0]
        r = await _reserve()
        assert r.tier is Tier.PREMIUM
        assert store.spend[TODAY] == [EST, CEILING, 0]
        assert store.leases == {USER.id: r.lease_id}

    async def test_premium_is_still_bound_by_its_own_caps(self, store, monkeypatch):
        _premium(monkeypatch)
        store.spend[TODAY] = [0, CEILING, 0]
        with pytest.raises(quota.DailyLimitReached):
            await _reserve(_ai(**{"quota.premium.found_daily": 0}))
        assert store.spend[TODAY] == [0, CEILING, 0]  # estimate given back

    async def test_count_cap_is_checked_before_the_cost_ceiling(self, store):
        """A second, independent limit: refused with scan_capacity even when
        the cost ceiling has room, and nothing is added to the spend."""
        store.extra_global = 10
        with pytest.raises(quota.GlobalCapacityReached):
            await _reserve(_ai(**{"scan.global_daily_limit": 10}))
        assert store.spend == {}

    async def test_ceiling_comes_from_ai_settings_default(self, store):
        with (
            patch.object(
                ai_settings.settings, "scan_free_daily_ceiling_micros", EST - 1
            ),
            pytest.raises(quota.CostCeilingReached),
        ):
            await _reserve(AISettings({}))


class TestReserveGivesTheEstimateBack:
    async def test_in_progress(self, store):
        store.leases[USER.id] = uuid.uuid4()
        with pytest.raises(quota.ScanInProgress):
            await _reserve()
        assert store.spend[TODAY][0] == 0

    async def test_lease_acquire_error(self, store):
        with (
            patch.object(quota, "acquire_lease", AsyncMock(side_effect=OSError("x"))),
            pytest.raises(OSError),
        ):
            await _reserve()
        assert store.spend[TODAY][0] == 0

    async def test_daily_refusal(self, store):
        with pytest.raises(quota.DailyLimitReached):
            await _reserve(_ai(**{"quota.free.found_daily": 0}))
        assert store.spend[TODAY][0] == 0

    async def test_lease_lost(self, store):
        with (
            patch.object(quota, "renew_lease", AsyncMock(return_value=False)),
            pytest.raises(quota.ScanInProgress),
        ):
            await _reserve()
        assert store.spend[TODAY][0] == 0

    async def test_cancellation(self, store):
        with (
            patch.object(
                quota, "take_unit", AsyncMock(side_effect=asyncio.CancelledError())
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await _reserve()
        assert store.spend[TODAY][0] == 0
        assert store.leases == {}


# ---------------------------------------------------------------------------
# quota.release: settle and alerts
# ---------------------------------------------------------------------------


def _settlement(cost, status="ok"):
    return breaker.Settlement(cost, "scan.standard", "gpt-4o-mini", status)


class TestSettle:
    async def test_settle_lower_than_estimate_releases_the_difference(self, store):
        r = await _reserve()
        await quota.release(AsyncMock(), r, None, _settlement(600))
        assert store.spend[TODAY] == [0, 600, 0]

    async def test_settle_higher_than_estimate_counts_the_real_cost(self, store):
        r = await _reserve()
        await quota.release(AsyncMock(), r, None, _settlement(EST * 3))
        assert store.spend[TODAY] == [0, EST * 3, 0]

    async def test_no_settlement_gives_the_whole_estimate_back(self, store):
        r = await _reserve()
        await quota.release(AsyncMock(), r)
        assert store.spend[TODAY] == [0, 0, 0]
        assert store.leases == {}

    async def test_spend_settles_in_the_same_commit_as_the_ledger_row(self, store):
        """Order inside the settle transaction: ledger row, cost settle, then
        the lease delete that commits."""
        r = await _reserve()
        calls = []
        db = AsyncMock()

        def execute(stmt):
            calls.append("ledger row")
            return MagicMock()  # RETURNING call_id: a row came back (inserted)

        db.execute.side_effect = execute
        real_settle, real_release = breaker.settle, quota.release_lease

        async def settle(*a):
            calls.append("cost settle")
            return await real_settle(*a)

        async def release_lease(*a):
            calls.append("lease delete + commit")
            return await real_release(*a)

        with (
            patch.object(breaker, "settle", settle),
            patch.object(quota, "release_lease", release_lease),
        ):
            await quota.release(db, r, object(), _settlement(600))
        assert calls == ["ledger row", "cost settle", "lease delete + commit"]

    async def test_dropped_ledger_row_still_settles_the_cost(self, store):
        r = await _reserve()
        db = AsyncMock()
        db.execute.side_effect = RuntimeError("row rejected")
        await quota.release(db, r, object(), _settlement(600))
        assert store.spend[TODAY] == [0, 600, 0]
        assert store.leases == {}

    async def test_failed_settle_is_retried_without_double_counting(self, store):
        """The first attempt fails before the commit (rolled back); the retry
        settles once. The fake has no rollback, so count the settle calls."""
        r = await _reserve()
        calls = []
        real = quota.release_lease

        async def flaky(*a):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("blip")
            return await real(*a)

        with patch.object(quota, "release_lease", flaky):
            await quota.release(AsyncMock(), r, None, _settlement(600))
        assert len(calls) == 2
        assert store.leases == {}


class TestSettleIdempotency:
    """quota.release settles the spend only when the ledger row was inserted
    by this execution (RETURNING call_id); a conflict means an earlier attempt
    already committed the row, the spend and the lease delete."""

    @staticmethod
    def _db(inserted: bool):
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = uuid.uuid4() if inserted else None
        db.execute.return_value = result
        return db

    async def test_inserted_row_settles(self, store):
        r = await _reserve()
        await quota.release(self._db(True), r, object(), _settlement(600))
        assert store.spend[TODAY] == [0, 600, 0]

    async def test_conflicting_row_skips_the_settle_but_releases_the_lease(self, store):
        r = await _reserve()
        capture = MagicMock()
        store.spend[TODAY][1] = 499_500  # this settle would cross 50 %
        with patch.object(breaker.sentry_sdk, "capture_message", capture):
            await quota.release(self._db(False), r, object(), _settlement(600))
        assert store.spend[TODAY] == [EST, 499_500, 0]  # untouched
        assert store.leases == {}
        capture.assert_not_called()

    async def test_no_ledger_row_still_settles(self, store):
        r = await _reserve()
        await quota.release(AsyncMock(), r, None, _settlement(600))
        assert store.spend[TODAY] == [0, 600, 0]


class TestAlerts:
    async def _settle_at(self, store, settled_before, cost, capture):
        store.spend.setdefault(TODAY, [0, 0, 0])[1] = settled_before
        r = await _reserve()
        with patch.object(breaker.sentry_sdk, "capture_message", capture):
            await quota.release(AsyncMock(), r, None, _settlement(cost))
        return r

    async def _settle_real(self, store, settled_before, cost):
        """Settle through a real SDK client; return the captured events."""
        store.spend.setdefault(TODAY, [0, 0, 0])[1] = settled_before
        r = await _reserve()
        with captured_events() as events:
            sentry_sdk.set_user({"id": str(USER.id)})  # as get_current_user
            sentry_sdk.set_tag("request_tag", "x")
            await quota.release(AsyncMock(), r, None, _settlement(cost))
        return events

    async def test_crossing_50_sends_one_event_with_fingerprint_and_tags(self, store):
        (event,) = await self._settle_real(store, 499_000, 1_000)
        assert event["level"] == "warning"
        assert event["fingerprint"] == ["ai-breaker", "2026-10-21", "50"]
        assert event["tags"]["feature"] == "scan.standard"
        assert event["tags"]["model"] == "gpt-4o-mini"
        assert event["tags"]["status"] == "ok"
        assert event["extra"]["settled_micros"] == 500_000
        assert event["extra"]["ceiling_micros"] == CEILING
        assert store.spend[TODAY][2] == 0b001

    async def test_event_carries_no_user_id_or_request_scope(self, store):
        (event,) = await self._settle_real(store, 499_000, 1_000)
        assert "user" not in event
        assert "request_tag" not in event["tags"]
        text = json.dumps(event)
        assert str(USER.id) not in text
        assert USER.email not in text

    async def test_alert_scope_does_not_leak_into_later_request_events(self, store):
        """Isolation both ways: a later event in the same request still has
        the user, and none of the alert's tags or fingerprint."""
        store.spend[TODAY] = [0, 499_000, 0]
        r = await _reserve()
        with captured_events() as events:
            sentry_sdk.set_user({"id": str(USER.id)})
            await quota.release(AsyncMock(), r, None, _settlement(1_000))
            sentry_sdk.capture_message("a later request event")
        alert, later = events
        assert "user" not in alert
        assert later["user"] == {"id": str(USER.id)}
        assert "feature" not in later.get("tags", {})
        assert "fingerprint" not in later
        assert "settled_micros" not in later.get("extra", {})

    async def test_one_settle_crossing_several_thresholds_sends_each_once(self, store):
        events = await self._settle_real(store, 0, 950_000)
        assert [e["fingerprint"][2] for e in events] == ["50", "75", "90"]
        assert store.spend[TODAY][2] == 0b111

    async def test_second_settle_above_the_same_threshold_sends_nothing(self, store):
        capture = MagicMock()
        await self._settle_at(store, 499_000, 1_000, capture)
        await self._settle_at(store, store.spend[TODAY][1], 1_000, capture)
        assert capture.call_count == 1

    async def test_already_alerted_threshold_is_not_claimed_again(self, store):
        store.spend[TODAY] = [0, 600_000, 0b001]
        claim = AsyncMock(return_value=[])
        with patch.object(breaker, "claim_alerts", claim):
            await self._settle_at(store, 600_000, 100, MagicMock())
        claim.assert_not_awaited()  # 50 % already sent; 75 % not reached

    async def test_lost_claim_sends_nothing(self, store):
        capture = MagicMock()
        with patch.object(breaker, "claim_alerts", AsyncMock(return_value=[])):
            await self._settle_at(store, 499_000, 1_000, capture)
        capture.assert_not_called()

    async def test_below_50_sends_nothing(self, store):
        capture = MagicMock()
        await self._settle_at(store, 0, 499_000, capture)
        capture.assert_not_called()

    async def test_refunded_call_sends_nothing(self, store):
        store.spend[TODAY] = [0, 600_000, 0]
        r = await _reserve()
        capture = MagicMock()
        with patch.object(breaker.sentry_sdk, "capture_message", capture):
            await quota.release(AsyncMock(), r)
        capture.assert_not_called()

    async def test_alert_failure_never_raises(self, store):
        capture = MagicMock(side_effect=RuntimeError("sentry down"))
        await self._settle_at(store, 499_000, 1_000, capture)
        assert store.leases == {}

    async def test_claim_failure_never_raises(self, store):
        capture = MagicMock()
        with patch.object(
            breaker, "claim_alerts", AsyncMock(side_effect=OSError("db down"))
        ):
            await self._settle_at(store, 499_000, 1_000, capture)
        capture.assert_not_called()


class TestGiveBackRetries:
    """A give-back with no ledger row must not be applied twice when its first
    attempt committed but raised (acknowledgement lost) and is retried."""

    async def test_pre_lease_cost_release_is_not_retried(self, store):
        """Lease busy: the estimate is released once, even if that release
        committed and then raised; another call's estimate stays reserved."""
        store.spend[TODAY] = [EST, 0, 0]  # another call's estimate
        store.leases[USER.id] = uuid.uuid4()  # a scan already in flight
        real = store.spend_release
        calls = []

        async def committed_then_raised(*a):
            calls.append(1)
            await real(*a)
            raise OSError("ack lost")

        with (
            patch.object(breaker, "release", committed_then_raised),
            pytest.raises(quota.ScanInProgress),
        ):
            await _reserve()
        assert len(calls) == 1
        assert store.spend[TODAY] == [EST, 0, 0]

    async def test_retried_refusal_release_gives_the_estimate_back_once(self, store):
        """No ledger row: the first attempt commits the spend release and the
        lease delete, then raises. The retry finds the lease gone and leaves
        the spend alone."""
        r = await _reserve()
        store.spend[TODAY][0] += EST  # another call's estimate
        real = store.release_lease
        calls = []

        async def committed_then_raised(*a):
            calls.append(1)
            deleted = await real(*a)
            if len(calls) == 1:
                raise OSError("ack lost")
            return deleted

        with patch.object(quota, "release_lease", committed_then_raised):
            await quota.release(AsyncMock(), r)
        assert len(calls) == 2
        assert store.spend[TODAY] == [EST, 0, 0]
        assert store.leases == {}

    async def test_retry_settles_when_the_first_attempt_did_not_commit(self, store):
        """The first attempt fails before anything is applied (rolled back):
        the lease is still held, so the retry releases the estimate."""
        r = await _reserve()
        spend_calls = []
        real_settle = store.spend_settle

        async def settle(*a):
            spend_calls.append(1)
            if len(spend_calls) == 1:
                raise OSError("blip before commit")
            return await real_settle(*a)

        with patch.object(breaker, "settle", settle):
            await quota.release(AsyncMock(), r)
        assert len(spend_calls) == 2
        assert store.spend[TODAY] == [0, 0, 0]
        assert store.leases == {}

    async def test_fallback_after_a_committed_settle_does_not_settle_again(self, store):
        """The settle with the ledger row committed, but both attempts raised;
        the lease-only fallback finds the lease gone and adds nothing."""
        r = await _reserve()
        store.spend[TODAY][0] += EST  # another call's estimate
        real = store.release_lease
        calls = []

        async def committed_then_raised(*a):
            calls.append(1)
            deleted = await real(*a)
            if len(calls) <= 2:
                raise OSError("ack lost")
            return deleted

        inserted, conflict = MagicMock(), MagicMock()
        inserted.scalar_one_or_none.return_value = uuid.uuid4()
        conflict.scalar_one_or_none.return_value = None  # row already committed
        db = AsyncMock()
        db.execute.side_effect = [inserted, conflict]
        with patch.object(quota, "release_lease", committed_then_raised):
            await quota.release(db, r, object(), _settlement(600))
        assert len(calls) == 3  # two settle attempts, then the fallback
        # The first attempt's settle is the only one: the second attempt's
        # insert conflicts and the fallback finds the lease gone.
        assert store.spend[TODAY] == [EST, 600, 0]
        assert store.leases == {}


# ---------------------------------------------------------------------------
# /scan
# ---------------------------------------------------------------------------


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), (30, 30, 200)).save(buf, format="JPEG")
    return buf.getvalue()


_JPEG = _jpeg()


def _file():
    return {"file": ("scan.jpg", io.BytesIO(_JPEG), "image/jpeg")}


@pytest.fixture
def client():
    from app.auth.dependencies import get_current_user
    from app.core.database import get_db

    async def _fake_db():
        yield AsyncMock()

    app.dependency_overrides[get_current_user] = lambda: USER
    app.dependency_overrides[get_db] = _fake_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def provider():
    with (
        patch("app.ai.gateway.OpenAIVisionProvider") as cls,
        patch("app.api.scan.EnrichmentService") as enrich,
        patch("app.api.scan.DeduplicationService") as dedup,
    ):
        cls.return_value.identify = AsyncMock(return_value=ProviderResult([]))
        enrich.return_value.enrich = AsyncMock(return_value=[])
        dedup.return_value.check = AsyncMock(return_value=[])
        yield cls


STANDARD_EST = get_feature("scan.standard").est_cost_ceiling_micros
ENHANCED_EST = get_feature("scan.enhanced").est_cost_ceiling_micros
USAGE = {"input_tokens": 1_000, "output_tokens": 500, "cache_read_tokens": 2_000}


def _at_ceiling(store):
    store.spend[quota.utc_today()] = [
        0,
        ai_settings.settings.scan_free_daily_ceiling_micros,
        0,
    ]


class TestScanEndpoint:
    def test_free_at_the_ceiling_gets_503_ai_budget_reached_without_provider(
        self, client, store, provider
    ):
        _at_ceiling(store)
        resp = client.post("/scan", files=_file())
        assert resp.status_code == 503
        assert resp.json() == {"detail": "ai_budget_reached"}
        provider.assert_not_called()
        assert store.leases == {} and store.count() == 0

    def test_premium_at_the_ceiling_reaches_the_provider(
        self, client, store, provider, monkeypatch
    ):
        _premium(monkeypatch)
        _at_ceiling(store)
        provider.return_value.identify = AsyncMock(
            return_value=ProviderResult([], **USAGE)
        )
        assert client.post("/scan", files=_file()).status_code == 200
        provider.return_value.identify.assert_awaited_once()
        reserved, settled = store.spend_today()
        assert reserved == 0
        # 1,000 * 0.15 + 500 * 0.60 + 2,000 * 0.075 = 600 micros added
        assert settled == ai_settings.settings.scan_free_daily_ceiling_micros + 600

    def test_estimate_is_the_features_registry_ceiling(self, client, store, provider):
        seen = []
        real = store.spend_reserve

        async def spy(db, day, est, ceiling, *, enforce):
            seen.append((est, enforce))
            return await real(db, day, est, ceiling, enforce=enforce)

        with patch.object(breaker, "reserve", spy):
            client.post("/scan", files=_file())
            # Enhanced scans are Premium-only (#533); Premium is not gated.
            with patch.object(
                entitlements, "tier_for", AsyncMock(return_value=Tier.PREMIUM)
            ):
                client.post("/scan", files=_file(), data={"enhanced": "true"})
        assert seen == [(STANDARD_EST, True), (ENHANCED_EST, False)]

    def test_answered_call_settles_its_priced_cost(self, client, store, provider):
        provider.return_value.identify = AsyncMock(
            return_value=ProviderResult([], **USAGE)
        )
        assert client.post("/scan", files=_file()).status_code == 200
        assert store.spend_today() == (0, 600)

    @pytest.mark.parametrize(
        "error",
        [ScanUnavailableError("down"), httpx.ConnectError("refused")],
    )
    def test_unavailable_provider_releases_the_estimate(
        self, client, store, provider, error
    ):
        provider.return_value.identify = AsyncMock(side_effect=error)
        assert client.post("/scan", files=_file()).status_code == 503
        assert store.spend_today() == (0, 0)
        assert store.count() == 0  # the count is refunded too

    def test_provider_timeout_keeps_the_estimate_as_spend(
        self, client, store, provider
    ):
        """The request reached the provider and is likely billed: the units
        are refunded but the estimate is settled."""
        from app.ai.providers.base import ProviderTimeoutError

        provider.return_value.identify = AsyncMock(
            side_effect=ProviderTimeoutError("slow")
        )
        assert client.post("/scan", files=_file()).status_code == 503
        assert store.spend_today() == (0, STANDARD_EST)
        assert store.count() == 0

    def test_kill_switch_in_the_gateway_releases_the_estimate(
        self, client, store, provider
    ):
        from app.ai.gateway import FeatureDisabledError

        with patch(
            "app.api.scan.run_feature",
            AsyncMock(side_effect=FeatureDisabledError("off")),
        ):
            assert client.post("/scan", files=_file()).status_code == 503
        assert store.spend_today() == (0, 0)

    def test_other_error_keeps_the_estimate_as_spend(self, client, store, provider):
        provider.return_value.identify = AsyncMock(side_effect=RuntimeError("boom"))
        client_ = TestClient(app, raise_server_exceptions=False)
        assert client_.post("/scan", files=_file()).status_code == 500
        assert store.spend_today() == (0, STANDARD_EST)
        assert store.count() == 1  # the units are kept too

    async def test_cancelled_call_keeps_the_estimate_as_spend(self, store, provider):
        from starlette.datastructures import Headers
        from starlette.datastructures import UploadFile as StarletteUploadFile

        from app.api import scan as scan_api

        async def hung(_image):
            await anyio.sleep(10)

        provider.return_value.identify = hung
        upload = StarletteUploadFile(
            file=io.BytesIO(_JPEG),
            filename="scan.jpg",
            headers=Headers({"content-type": "image/jpeg"}),
        )
        with (
            patch.object(scan_api.settings, "turnstile_required", False),
            anyio.move_on_after(0.3) as scope,
        ):
            await scan_api.scan.__wrapped__(
                request=MagicMock(),
                file=upload,
                current_user=USER,
                db=AsyncMock(),
                cf_turnstile_response=None,
                enhanced=False,
            )
        assert scope.cancelled_caught
        assert store.spend_today() == (0, STANDARD_EST)
        assert store.leases == {}

    def test_unpriceable_result_settles_the_estimate(self, client, store, provider):
        with patch(
            "app.api.scan.cost_micros", MagicMock(side_effect=LookupError("no price"))
        ):
            assert client.post("/scan", files=_file()).status_code == 200
        assert store.spend_today() == (0, STANDARD_EST)


# ---------------------------------------------------------------------------
# Provider timeouts: connected (billed) vs never connected (not billed)
# ---------------------------------------------------------------------------


def _client_raising(exc):
    client = AsyncMock()
    client.post = AsyncMock(side_effect=exc)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return patch(
        "app.ai.providers.openai_vision.httpx.AsyncClient", return_value=client
    )


class TestProviderTimeoutKinds:
    @pytest.mark.parametrize(
        "exc", [httpx.ConnectTimeout("connect"), httpx.PoolTimeout("pool")]
    )
    async def test_no_connection_timeouts_are_connect_errors(self, exc):
        from app.ai.providers.base import ProviderTimeoutError
        from app.ai.providers.openai_vision import OpenAIVisionProvider

        provider = OpenAIVisionProvider(get_feature("scan.standard"))
        with _client_raising(exc), pytest.raises(httpx.ConnectError) as raised:
            await provider.identify(b"img")
        assert not isinstance(raised.value, ProviderTimeoutError)
        assert raised.value.__cause__ is exc

    @pytest.mark.parametrize(
        "exc", [httpx.ReadTimeout("read"), httpx.WriteTimeout("write")]
    )
    async def test_connected_timeouts_are_provider_timeouts(self, exc):
        from app.ai.providers.base import ProviderTimeoutError
        from app.ai.providers.openai_vision import OpenAIVisionProvider

        provider = OpenAIVisionProvider(get_feature("scan.standard"))
        with _client_raising(exc), pytest.raises(ProviderTimeoutError):
            await provider.identify(b"img")

    @pytest.mark.parametrize(
        "exc", [httpx.ConnectTimeout("connect"), httpx.PoolTimeout("pool")]
    )
    def test_no_connection_timeout_refunds_and_releases_the_estimate(
        self, client, store, exc
    ):
        rows = []
        real = ledger.usage_insert

        def spy(**kw):
            rows.append(kw["status"])
            return real(**kw)

        with (
            _client_raising(exc),
            patch.object(ledger, "usage_insert", spy),
            patch("app.api.scan.EnrichmentService"),
            patch("app.api.scan.DeduplicationService"),
        ):
            resp = client.post("/scan", files=_file())
        assert resp.json() == {"detail": "scan_unavailable"}
        assert rows == ["refunded"]
        assert store.spend_today() == (0, 0)
        assert store.count() == 0

    @pytest.mark.parametrize(
        "exc", [httpx.ReadTimeout("read"), httpx.WriteTimeout("write")]
    )
    def test_connected_timeout_refunds_units_but_keeps_the_estimate(
        self, client, store, exc
    ):
        with (
            _client_raising(exc),
            patch("app.api.scan.EnrichmentService"),
            patch("app.api.scan.DeduplicationService"),
        ):
            resp = client.post("/scan", files=_file())
        assert resp.json() == {"detail": "scan_unavailable"}
        assert store.spend_today() == (0, STANDARD_EST)
        assert store.count() == 0

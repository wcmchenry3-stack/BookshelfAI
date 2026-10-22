"""Enhanced scans: Premium-only, own monthly allowance, charge-only-when-books-
found, zero-result tracking (#533). Provider mocked at the gateway boundary;
ai_quota / ai_inflight / ai_enhanced_zero_results faked in memory
(tests/unit/_quota_fake.py). The Postgres behaviour is in
tests/integration/test_enhanced_quota_db.py."""

import asyncio
import io
import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sentry_sdk
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from app.ai import entitlements, quota
from app.ai import settings as ai_settings
from app.ai.entitlements import Tier
from app.ai.providers.base import ProviderResult
from app.core.config import Settings
from app.core.sentry_context import set_sentry_user
from app.main import app
from app.models.user import User
from app.services.book_identifier import BookCandidate, ScanUnavailableError
from app.services.token_cleanup import cleanup_expired_tokens
from tests.unit._quota_fake import FakeQuotaStore
from tests.unit._sentry_capture import captured_app_events, captured_events

USER = User(
    id=uuid.UUID("00000000-0000-0000-0000-000000000533"),
    email="enhanced@example.com",
)
DUNE = BookCandidate(title="Dune", author="Frank Herbert", confidence=0.9)


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), (90, 30, 200)).save(buf, format="JPEG")
    return buf.getvalue()


_JPEG = _jpeg()


def _file():
    return {"file": ("scan.jpg", io.BytesIO(_JPEG), "image/jpeg")}


@pytest.fixture
def store():
    s = FakeQuotaStore(default_user=USER.id)
    with s.installed():
        yield s


@pytest.fixture
def client():
    from app.auth.dependencies import get_current_user
    from app.core.database import get_db

    async def _fake_db():
        db = AsyncMock()
        db.execute = AsyncMock(return_value=MagicMock())
        yield db

    app.dependency_overrides[get_current_user] = lambda: USER
    app.dependency_overrides[get_db] = _fake_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def provider():
    """No books by default; ``books(True)`` makes the provider find Dune."""
    with (
        patch("app.ai.gateway.OpenAIVisionProvider") as cls,
        patch("app.api.scan.EnrichmentService") as enrich,
        patch("app.api.scan.DeduplicationService") as dedup,
    ):
        cls.return_value.identify = AsyncMock(return_value=ProviderResult([]))

        async def _enrich(candidates, limit):
            return [_book(c) for c in candidates]

        enrich.return_value.enrich = AsyncMock(side_effect=_enrich)
        dedup.return_value.check = AsyncMock(side_effect=lambda _d, _u, b: b)
        yield cls


def _book(candidate):
    from app.schemas.book import EnrichedBook

    return EnrichedBook(title=candidate.title, author=candidate.author, confidence=0.9)


def _finds_books(provider, found=True):
    provider.return_value.identify = AsyncMock(
        return_value=ProviderResult([DUNE] if found else [])
    )


@pytest.fixture
def premium(monkeypatch):
    monkeypatch.setattr(entitlements, "tier_for", AsyncMock(return_value=Tier.PREMIUM))


def _settings(**rows):
    ai_settings.reset_cache(
        {
            "quota.premium.enhanced_monthly": 20,
            "scan.enhanced.zero_result_threshold": 3,
            "scan.enhanced.zero_result_window_hours": 24,
            "scan.enhanced.zero_result_action": "charge",
            **rows,
        }
    )


def _enhanced(client):
    return client.post("/scan", files=_file(), data={"enhanced": "true"})


def _enhanced_used(store) -> int:
    return store.count(feature=quota.ENHANCED_QUOTA_FEATURE, kind="month")


# ---------------------------------------------------------------------------
# Pure maths
# ---------------------------------------------------------------------------


class TestThresholdMaths:
    @pytest.mark.parametrize(
        "count,threshold,charged",
        [
            (2, 3, False),  # threshold - 1: still free
            (3, 3, True),  # = threshold: charged
            (4, 3, True),  # threshold + 1: charged
            (0, 0, True),  # threshold 0: every zero-result is charged
            (0, 1, False),
            (19, 20, False),
            (20, 20, True),
        ],
    )
    def test_charged_at_or_above_threshold(self, count, threshold, charged):
        assert quota.zero_result_charged(count, threshold) is charged


class TestRetryAfter:
    NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)

    def test_seconds_until_the_row_leaves(self):
        leaves = self.NOW + timedelta(seconds=3600)
        assert quota.retry_after_seconds(leaves, 24, now=self.NOW) == 3600

    def test_rounds_up_partial_seconds(self):
        leaves = self.NOW + timedelta(seconds=10, milliseconds=200)
        assert quota.retry_after_seconds(leaves, 24, now=self.NOW) == 11

    @pytest.mark.parametrize("delta", [0, -5])
    def test_at_least_one_second(self, delta):
        leaves = self.NOW + timedelta(seconds=delta)
        assert quota.retry_after_seconds(leaves, 24, now=self.NOW) == 1

    def test_without_a_row_the_whole_window(self):
        # Threshold 0 with action throttle: no row needs to leave.
        assert quota.retry_after_seconds(None, 6, now=self.NOW) == 6 * 3600


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class TestSettings:
    def test_enhanced_allowance_is_premium_only(self):
        ai = ai_settings.AISettings({"quota.premium.enhanced_monthly": 9})
        assert ai.enhanced_monthly_allowance("premium") == 9
        assert ai.enhanced_monthly_allowance("free") == 0
        assert ai.enhanced_monthly_allowance("anything") == 0

    def test_defaults(self):
        ai = ai_settings.AISettings({})
        assert ai.zero_result_threshold == 3
        assert ai.zero_result_window_hours == 24
        assert ai.zero_result_action == "charge"

    @pytest.mark.parametrize(
        "key,bad",
        [
            ("scan.enhanced.zero_result_threshold", 21),
            ("scan.enhanced.zero_result_window_hours", 0),
            ("scan.enhanced.zero_result_action", "block"),
            ("quota.premium.enhanced_monthly", 10_001),
        ],
    )
    def test_invalid_stored_value_is_ignored_with_warning(self, key, bad):
        with patch.object(ai_settings.sentry_sdk, "capture_message") as capture:
            ai_settings.reset_cache({key: bad})
            ai = ai_settings.AISettings(ai_settings._cache.rows)
        assert key not in ai.rows
        assert ai.get(key) == ai_settings.DEFINITIONS[key].default()
        capture.assert_called_once()
        assert str(bad) not in capture.call_args.args[0]

    @pytest.mark.parametrize("value", ["block", "21", "-1"])
    def test_cli_refuses_invalid_values(self, value, capsys):
        key = (
            "scan.enhanced.zero_result_action"
            if value == "block"
            else "scan.enhanced.zero_result_threshold"
        )
        assert ai_settings.main(["set", key, value]) == 2
        assert "error:" in capsys.readouterr().err

    @pytest.mark.parametrize("bad", ["-1", "10001", "abc"])
    def test_env_seed_is_validated_at_boot(self, bad, monkeypatch):
        monkeypatch.setenv("SCAN_PREMIUM_ENHANCED_MONTHLY_LIMIT", bad)
        with pytest.raises(ValidationError):
            Settings(database_url="postgresql://u:p@localhost/db")

    def test_env_seed_default(self, monkeypatch):
        monkeypatch.delenv("SCAN_PREMIUM_ENHANCED_MONTHLY_LIMIT", raising=False)
        s = Settings(database_url="postgresql://u:p@localhost/db", _env_file=None)
        assert s.scan_premium_enhanced_monthly_limit == 20


# ---------------------------------------------------------------------------
# POST /scan: refusals before the provider
# ---------------------------------------------------------------------------


class TestPremiumOnly:
    def test_free_enhanced_is_403_before_anything_is_reserved(
        self, client, store, provider
    ):
        _settings()
        resp = _enhanced(client)
        assert resp.status_code == 403
        assert resp.json() == {"detail": "enhanced_requires_premium"}
        provider.assert_not_called()
        assert store.rows == {} and store.leases == {} and store.releases == 0
        assert store.spend == {}  # no cost estimate reserved

    def test_free_standard_scan_still_works(self, client, store, provider):
        _settings()
        assert client.post("/scan", files=_file()).status_code == 200

    def test_kill_switch_answers_before_the_tier(self, client, store, provider):
        _settings(**{"scan.enhanced.enabled": False})
        assert _enhanced(client).json() == {"detail": "enhanced_scan_disabled"}

    def test_premium_enhanced_reaches_the_provider(
        self, client, store, provider, premium
    ):
        _settings()
        _finds_books(provider)
        resp = _enhanced(client)
        assert resp.status_code == 200
        provider.return_value.identify.assert_awaited_once()


class TestEnhancedAllowance:
    def test_reserves_and_keeps_a_unit_when_books_found(
        self, client, store, provider, premium
    ):
        _settings(**{"quota.premium.enhanced_monthly": 5})
        _finds_books(provider)
        resp = _enhanced(client)
        assert resp.json()["enhanced_scans_remaining"] == 4
        assert _enhanced_used(store) == 1
        # And the shared units, as for any scan (OD-1).
        assert store.count() == 1 and store.count(kind="month") == 1

    def test_standard_scan_does_not_use_the_enhanced_allowance(
        self, client, store, provider, premium
    ):
        _settings(**{"quota.premium.enhanced_monthly": 5})
        _finds_books(provider)
        resp = client.post("/scan", files=_file())
        assert resp.json()["enhanced_scans_remaining"] == 5
        assert _enhanced_used(store) == 0

    def test_free_response_has_no_remaining(self, client, store, provider):
        _settings()
        assert (
            client.post("/scan", files=_file()).json()["enhanced_scans_remaining"]
            is None
        )

    def test_exhausted_is_402_and_gives_everything_back(
        self, client, store, provider, premium
    ):
        _settings(**{"quota.premium.enhanced_monthly": 1})
        _finds_books(provider)
        assert _enhanced(client).status_code == 200
        resp = _enhanced(client)
        assert resp.status_code == 402
        assert resp.json()["detail"] == "enhanced_limit_reached"
        assert (resp.json()["limit"], resp.json()["used"]) == (1, 1)
        assert provider.return_value.identify.await_count == 1
        # The second request's shared units, lease and cost were given back.
        assert store.count() == 1 and store.count(kind="month") == 1
        assert store.leases == {}
        assert store.spend_today()[0] == 0

    def test_zero_allowance_refuses_before_the_provider(
        self, client, store, provider, premium
    ):
        _settings(**{"quota.premium.enhanced_monthly": 0})
        resp = _enhanced(client)
        assert resp.json()["detail"] == "enhanced_limit_reached"
        provider.assert_not_called()

    @pytest.mark.parametrize(
        "error", [ScanUnavailableError("down"), ConnectionRefusedError()]
    )
    def test_provider_unavailable_refunds_and_writes_no_row(
        self, client, store, provider, premium, error
    ):
        import httpx

        _settings()
        if isinstance(error, ConnectionRefusedError):
            error = httpx.ConnectError("refused")
        provider.return_value.identify = AsyncMock(side_effect=error)
        assert _enhanced(client).json() == {"detail": "scan_unavailable"}
        assert _enhanced_used(store) == 0
        assert store.count() == 0
        assert store.zero_results == {}

    def test_provider_error_keeps_the_unit_and_writes_no_row(
        self, client, store, provider, premium
    ):
        _settings()
        provider.return_value.identify = AsyncMock(side_effect=RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            _enhanced(client)
        # Kept like the shared units (the provider may have been paid).
        assert _enhanced_used(store) == 1
        assert store.zero_results == {}


# ---------------------------------------------------------------------------
# Charge decision and zero-result tracking
# ---------------------------------------------------------------------------


class TestChargeDecision:
    def test_zero_results_up_to_threshold_are_free_then_charged(
        self, client, store, provider, premium
    ):
        _settings(**{"scan.enhanced.zero_result_threshold": 3})
        for n in range(1, 4):
            assert _enhanced(client).status_code == 200
            assert _enhanced_used(store) == 0, n  # not charged
            assert len(store.zero_results[USER.id]) == n
        # The fourth zero-result is charged and writes no row.
        resp = _enhanced(client)
        assert resp.status_code == 200
        assert _enhanced_used(store) == 1
        assert len(store.zero_results[USER.id]) == 3
        assert resp.json()["enhanced_scans_remaining"] == 19
        # The shared units are counted for every scan, empty or not.
        assert store.count() == 4

    def test_books_found_after_threshold_are_charged_as_usual(
        self, client, store, provider, premium
    ):
        _settings(**{"scan.enhanced.zero_result_threshold": 1})
        _enhanced(client)
        _finds_books(provider)
        _enhanced(client)
        assert _enhanced_used(store) == 1

    def test_threshold_zero_charge_charges_every_zero_result(
        self, client, store, provider, premium
    ):
        _settings(**{"scan.enhanced.zero_result_threshold": 0})
        _enhanced(client)
        _enhanced(client)
        assert _enhanced_used(store) == 2
        assert store.zero_results.get(USER.id, []) == []

    def test_row_carries_the_ledger_call_id(self, client, store, provider, premium):
        _settings()
        _enhanced(client)
        (ledger_id,) = store.zero_results[USER.id]
        assert isinstance(ledger_id, uuid.UUID)

    def test_users_are_counted_separately(self, client, store, provider, premium):
        _settings(**{"scan.enhanced.zero_result_threshold": 1})
        other = uuid.uuid4()
        store.zero_results[other] = [None] * 5
        _enhanced(client)
        assert _enhanced_used(store) == 0  # USER's first zero-result is free


class TestThrottle:
    def test_at_threshold_next_enhanced_is_429_before_the_provider(
        self, client, store, provider, premium
    ):
        _settings(
            **{
                "scan.enhanced.zero_result_threshold": 2,
                "scan.enhanced.zero_result_action": "throttle",
                "scan.enhanced.zero_result_window_hours": 6,
            }
        )
        assert _enhanced(client).status_code == 200
        assert _enhanced(client).status_code == 200
        assert _enhanced_used(store) == 0
        calls = provider.return_value.identify.await_count
        before = dict(store.rows)
        resp = _enhanced(client)
        assert resp.status_code == 429
        assert resp.json() == {"detail": "enhanced_throttled"}
        assert 1 <= int(resp.headers["Retry-After"]) <= 6 * 3600
        assert provider.return_value.identify.await_count == calls
        # Nothing left reserved: units, lease and cost all given back.
        assert store.rows == before
        assert store.leases == {}
        assert store.spend_today()[0] == 0

    def test_standard_scans_are_not_throttled(self, client, store, provider, premium):
        _settings(
            **{
                "scan.enhanced.zero_result_threshold": 1,
                "scan.enhanced.zero_result_action": "throttle",
            }
        )
        _enhanced(client)
        assert client.post("/scan", files=_file()).status_code == 200

    def test_threshold_zero_throttle_refuses_every_enhanced_scan(
        self, client, store, provider, premium
    ):
        _settings(
            **{
                "scan.enhanced.zero_result_threshold": 0,
                "scan.enhanced.zero_result_action": "throttle",
            }
        )
        resp = _enhanced(client)
        assert resp.status_code == 429
        assert resp.headers["Retry-After"] == str(24 * 3600)
        provider.assert_not_called()

    def test_throttle_is_after_the_premium_check(self, client, store, provider):
        _settings(
            **{
                "scan.enhanced.zero_result_threshold": 0,
                "scan.enhanced.zero_result_action": "throttle",
            }
        )
        assert _enhanced(client).json() == {"detail": "enhanced_requires_premium"}


# ---------------------------------------------------------------------------
# Settle: gating and the warning
# ---------------------------------------------------------------------------


def _reservation(**kw) -> quota.Reservation:
    today = quota.utc_today()
    base = {
        "user_id": USER.id,
        "feature": quota.SCAN_QUOTA_FEATURE,
        "lease_id": uuid.uuid4(),
        "day": today,
        "month": quota.month_start(today),
        "tier": Tier.PREMIUM,
        "allowance": quota.Allowance(30, 300),
        "enhanced": True,
        "enhanced_allowance": 20,
        "zero_result_threshold": 3,
        "zero_result_window_hours": 24,
        "zero_result_action": "charge",
    }
    base.update(kw)
    return quota.Reservation(**base)


class TestSettleGate:
    @pytest.fixture
    def record(self):
        record = AsyncMock(return_value=1)
        with (
            patch.object(quota, "record_zero_result", record),
            patch.object(quota, "release_lease", AsyncMock(return_value=True)),
        ):
            yield record

    def _db(self, inserted=True):
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = uuid.uuid4() if inserted else None
        db.execute = AsyncMock(return_value=result)
        return db

    async def test_runs_with_the_ledger_row(self, record):
        ledger_id = uuid.uuid4()
        await quota.release(
            self._db(), _reservation(), MagicMock(), None, quota.ZeroResult(ledger_id)
        )
        record.assert_awaited_once()
        assert record.await_args.args[2] == ledger_id

    async def test_retried_settle_whose_row_exists_does_not_record_again(self, record):
        """A settle retried after a commit whose acknowledgement was lost:
        the ledger insert returns nothing, so the decision is not applied
        again."""
        await quota.release(
            self._db(inserted=False),
            _reservation(),
            MagicMock(),
            None,
            quota.ZeroResult(uuid.uuid4()),
        )
        record.assert_not_awaited()

    async def test_retry_without_ledger_row_is_gated_on_the_lease(self, record):
        calls = []

        async def flaky_lease(db, uid, lease_id):
            calls.append(lease_id)
            if len(calls) == 1:
                raise ConnectionError("ack lost")
            return False  # the first attempt committed; the lease is gone

        with patch.object(quota, "release_lease", flaky_lease):
            await quota.release(
                self._db(), _reservation(), None, None, quota.ZeroResult(None)
            )
        # Attempt 1 recorded (then rolled back), attempt 2 found no lease.
        assert record.await_count == 1

    async def test_no_decision_without_zero_result(self, record):
        await quota.release(self._db(), _reservation(), MagicMock(), None, None)
        record.assert_not_awaited()

    async def test_no_decision_for_a_standard_reservation(self, record):
        await quota.release(
            self._db(),
            _reservation(enhanced=False),
            MagicMock(),
            None,
            quota.ZeroResult(None),
        )
        record.assert_not_awaited()

    @pytest.mark.parametrize("count,sent", [(2, False), (3, True), (None, False)])
    async def test_warning_only_when_the_count_reaches_the_threshold(
        self, record, count, sent
    ):
        record.return_value = count
        with patch.object(quota, "send_zero_result_warning") as warn:
            await quota.release(
                self._db(), _reservation(), MagicMock(), None, quota.ZeroResult(None)
            )
        assert warn.called is sent
        if sent:
            assert warn.call_args.kwargs == {
                "count": 3,
                "threshold": 3,
                "window_hours": 24,
                "action": "charge",
            }

    async def test_no_warning_when_the_settle_failed(self, record):
        record.return_value = 3

        async def broken(db, uid, lease_id):
            raise ConnectionError("down")

        with (
            patch.object(quota, "release_lease", broken),
            patch.object(quota, "send_zero_result_warning") as warn,
        ):
            await quota.release(
                self._db(), _reservation(), MagicMock(), None, quota.ZeroResult(None)
            )
        warn.assert_not_called()

    async def test_record_failure_is_swallowed_and_charges(self):
        db = AsyncMock()
        db.begin_nested = MagicMock(side_effect=RuntimeError("savepoint failed"))
        assert await quota.record_zero_result(db, _reservation(), None) is None

    def test_endpoint_sends_one_warning_per_reach(
        self, client, store, provider, premium
    ):
        _settings(**{"scan.enhanced.zero_result_threshold": 2})
        with patch.object(quota, "send_zero_result_warning") as warn:
            for _ in range(4):
                _enhanced(client)
        warn.assert_called_once()


class TestWarningContent:
    def test_event_has_only_the_fixed_fields(self):
        with captured_events() as events:
            set_sentry_user(USER.id)
            sentry_sdk.set_tag("endpoint", "/scan")
            sentry_sdk.add_breadcrumb(message=f"user {USER.email}")
            quota.send_zero_result_warning(
                count=3, threshold=3, window_hours=24, action="throttle"
            )
            sentry_sdk.capture_message("later")
        warning, later = events
        assert warning["level"] == "warning"
        assert warning["fingerprint"] == ["ai-enhanced-zero-results", "throttle"]
        assert warning["tags"] == {"feature": "scan.enhanced", "action": "throttle"}
        assert warning["extra"] == {"count": 3, "threshold": 3, "window_hours": 24}
        text = json.dumps(warning)
        assert "user" not in warning
        assert str(USER.id) not in text and USER.email not in text
        assert "breadcrumbs" not in warning or not warning["breadcrumbs"].get("values")
        # The request's later events are unchanged.
        assert later["user"] == {"id": str(USER.id)}
        assert later["tags"]["endpoint"] == "/scan"
        assert "action" not in later["tags"]

    def test_inside_a_real_request_carries_no_request_data(self):
        from fastapi import FastAPI, Request

        from app.core.sentry_context import SentryContextMiddleware

        mini = FastAPI()
        mini.add_middleware(SentryContextMiddleware)

        @mini.post("/scan")
        async def _scan(request: Request):
            await request.form()
            set_sentry_user(USER.id)
            sentry_sdk.add_breadcrumb(message="lookup", category="test")
            sentry_sdk.set_context("request_ctx", {"x": "y"})
            quota.send_zero_result_warning(
                count=3, threshold=3, window_hours=24, action="charge"
            )
            sentry_sdk.capture_message("later in the same request")
            return {"ok": True}

        with captured_app_events() as events:
            resp = TestClient(mini).post(
                "/scan",
                headers={
                    "CF-Connecting-IP": "198.51.100.7",
                    "User-Agent": "Probe/1.0 (iPhone)",
                },
                data={"enhanced": "true", "note": "shelf by the window"},
            )
        assert resp.status_code == 200
        warning, later = events
        assert warning["fingerprint"][0] == "ai-enhanced-zero-results"
        for key in ("request", "user", "breadcrumbs", "transaction"):
            assert key not in warning, key
        assert set(warning.get("contexts", {})) <= {"trace", "runtime", "os"}
        text = json.dumps(warning)
        for leak in ("198.51.100.7", "Probe/1.0", "shelf by the window"):
            assert leak not in text
        assert str(USER.id) not in text
        assert later["request"]["headers"]["cf-connecting-ip"] == "198.51.100.7"
        assert later["user"] == {"id": str(USER.id)}

    def test_never_raises(self):
        with patch.object(
            quota.alerts, "capture_service_warning", side_effect=RuntimeError("x")
        ):
            quota.send_zero_result_warning(
                count=1, threshold=1, window_hours=1, action="charge"
            )


# ---------------------------------------------------------------------------
# GET /scan/usage
# ---------------------------------------------------------------------------


class TestUsage:
    async def test_premium_sees_enhanced_usage_and_limit(self, premium):
        db = AsyncMock()
        db.execute.return_value = MagicMock(
            all=lambda: [(quota.ENHANCED_QUOTA_FEATURE, "month", 1)]
        )
        usage = await quota.usage_for(
            db,
            user=USER,
            feature=quota.SCAN_QUOTA_FEATURE,
            ai=ai_settings.AISettings({"quota.premium.enhanced_monthly": 7}),
        )
        assert (usage.enhanced_used_month, usage.enhanced_monthly_limit) == (1, 7)
        assert usage.used_month == 0  # the enhanced key is not the shared one

    def test_endpoint_returns_the_enhanced_fields(self, client, premium):
        with patch.object(
            quota,
            "usage_for",
            AsyncMock(
                return_value=quota.Usage(
                    1,
                    30,
                    2,
                    300,
                    quota.utc_today(),
                    enhanced_used_month=2,
                    enhanced_monthly_limit=20,
                )
            ),
        ):
            body = client.get("/scan/usage").json()
        assert (body["enhanced_used_month"], body["enhanced_monthly_limit"]) == (
            2,
            20,
        )

    async def test_free_limit_is_zero(self):
        db = AsyncMock()
        db.execute.return_value = MagicMock(all=list)
        usage = await quota.usage_for(
            db,
            user=USER,
            feature=quota.SCAN_QUOTA_FEATURE,
            ai=ai_settings.AISettings({}),
        )
        assert usage.enhanced_monthly_limit == 0


# ---------------------------------------------------------------------------
# Daily cleanup
# ---------------------------------------------------------------------------


def _session_factory():
    result = MagicMock(rowcount=0)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=session), session


class TestCleanup:
    async def _run(self, purge, factory):
        with (
            patch("app.services.token_cleanup.AsyncSessionLocal", factory),
            patch("app.services.token_cleanup.purge_old_scan_usage", AsyncMock()),
            patch("app.services.token_cleanup.purge_old_quota", AsyncMock()),
            patch(
                "app.services.token_cleanup.rollup_and_purge",
                AsyncMock(return_value=(0, 0)),
            ),
            patch("app.services.token_cleanup.purge_old_spend", AsyncMock()),
            patch("app.services.token_cleanup.purge_old_zero_results", purge),
            patch("app.services.token_cleanup._INITIAL_DELAY_SECONDS", 0),
            patch("app.services.token_cleanup._INTERVAL_SECONDS", 9999),
            pytest.raises(asyncio.CancelledError),
        ):
            task = asyncio.create_task(cleanup_expired_tokens())
            for _ in range(8):
                await asyncio.sleep(0)
            task.cancel()
            await task

    async def test_run_purges_zero_results(self):
        factory, _ = _session_factory()
        purge = AsyncMock(return_value=2)
        await self._run(purge, factory)
        purge.assert_awaited_once()

    async def test_failure_does_not_skip_token_cleanup(self, caplog):
        import logging

        factory, session = _session_factory()
        with caplog.at_level(logging.ERROR, logger="app.services.token_cleanup"):
            await self._run(AsyncMock(side_effect=ConnectionError("down")), factory)
        assert "enhanced zero results" in caplog.text
        assert "refresh_tokens" in str(session.execute.call_args[0][0])

    async def test_purge_is_batched_and_bounded(self):
        db = AsyncMock()
        db.execute.return_value = MagicMock(rowcount=quota.PURGE_BATCH_SIZE)
        deleted = await quota.purge_old_zero_results(db)
        assert db.execute.await_count == quota.PURGE_MAX_BATCHES
        assert deleted == quota.PURGE_BATCH_SIZE * quota.PURGE_MAX_BATCHES
        sql = str(db.execute.await_args.args[0])
        assert "ai_enhanced_zero_results" in sql and "LIMIT" in sql

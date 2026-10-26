"""Enhanced scans: Premium-only, the shared daily allowance (no enhanced
monthly allowance since #582), zero-result tracking (#533). Provider mocked at the gateway boundary;
ai_quota / ai_inflight / ai_enhanced_zero_results faked in memory
(tests/unit/_quota_fake.py). The Postgres behaviour is in
tests/integration/test_enhanced_quota_db.py."""

import asyncio
import io
import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest
import sentry_sdk
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

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


@pytest.fixture(autouse=True)
def _no_scan_request_count():
    """#586: /scan counts each request with a DB upsert before any other
    work, which this file's fake DB cannot run. Stubbed here; the count is
    covered by tests/unit/test_scan_request_limit.py and tests/limits/."""
    from unittest.mock import AsyncMock, patch

    with patch("app.api.scan._count_scan_request", new=AsyncMock()):
        yield


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
            "quota.premium.found_daily": 30,
            "scan.enhanced.zero_result_threshold": 3,
            "scan.enhanced.zero_result_window_hours": 24,
            "scan.enhanced.zero_result_action": "charge",
            **rows,
        }
    )


def _enhanced(client):
    return client.post("/scan", files=_file(), data={"enhanced": "true"})


def _month_rows(store) -> int:
    """Month-window rows written (#582: there must be none)."""
    return sum(1 for (_, _, kind, _) in store.rows if kind == "month")


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
    def test_enhanced_monthly_key_is_retired(self):
        """#582: no enhanced monthly allowance; a stored row is dropped
        without a warning and nothing exposes it."""
        assert "quota.premium.enhanced_monthly" not in ai_settings.DEFINITIONS
        assert "quota.premium.enhanced_monthly" in ai_settings.RETIRED_KEYS
        assert not hasattr(ai_settings.AISettings, "enhanced_monthly_allowance")
        with patch.object(ai_settings.sentry_sdk, "capture_message") as capture:
            ai_settings.reset_cache({"quota.premium.enhanced_monthly": 9})
        assert "quota.premium.enhanced_monthly" not in ai_settings._cache.rows
        capture.assert_not_called()

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

    @pytest.mark.parametrize(
        "name",
        [
            "SCAN_FREE_MONTHLY_LIMIT",
            "SCAN_PREMIUM_MONTHLY_LIMIT",
            "SCAN_PREMIUM_ENHANCED_MONTHLY_LIMIT",
        ],
    )
    def test_retired_process_env_var_does_not_break_boot(self, monkeypatch, name):
        """#594 (the Render case): the settings fields are gone, but an
        environment (process env var) that still sets one must boot. Only a
        key in the .env file that matches no field is refused."""
        monkeypatch.setenv(name, "10")
        s = Settings(database_url="postgresql://u:p@localhost/db", _env_file=None)
        assert not hasattr(s, name.lower())
        assert "quota.premium.enhanced_monthly" not in ai_settings.seed_values()

    def test_retired_key_in_the_env_file_is_refused(self, tmp_path):
        """Documented upgrade note (docs/deployment.md): a local .env that
        still lists a retired key must drop it."""
        env_file = tmp_path / ".env"
        env_file.write_text("SCAN_FREE_MONTHLY_LIMIT=10\n")
        with pytest.raises(ValidationError):
            Settings(database_url="postgresql://u:p@localhost/db", _env_file=env_file)


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


class TestEnhancedUsesTheDailyUnit:
    """#582: an enhanced scan takes the shared daily unit and nothing else;
    ``enhanced_scans_remaining`` is the daily allowance left (Premium)."""

    def test_reserves_and_keeps_the_daily_unit_when_books_found(
        self, client, store, provider, premium
    ):
        _settings(**{"quota.premium.found_daily": 5})
        _finds_books(provider)
        resp = _enhanced(client)
        assert resp.json()["enhanced_scans_remaining"] == 4
        assert store.count() == 1
        assert _month_rows(store) == 0

    def test_standard_and_enhanced_share_the_daily_allowance(
        self, client, store, provider, premium
    ):
        _settings(**{"quota.premium.found_daily": 5})
        _finds_books(provider)
        resp = client.post("/scan", files=_file())
        assert resp.json()["enhanced_scans_remaining"] == 4
        assert _enhanced(client).json()["enhanced_scans_remaining"] == 3
        assert store.count() == 2
        assert _month_rows(store) == 0

    def test_free_response_has_no_remaining(self, client, store, provider):
        _settings()
        assert (
            client.post("/scan", files=_file()).json()["enhanced_scans_remaining"]
            is None
        )

    def test_daily_exhausted_is_429_and_gives_everything_back(
        self, client, store, provider, premium
    ):
        _settings(**{"quota.premium.found_daily": 1})
        _finds_books(provider)
        first = _enhanced(client)
        assert first.status_code == 200
        assert first.json()["enhanced_scans_remaining"] == 0
        resp = _enhanced(client)
        assert resp.status_code == 429
        assert resp.json() == {"detail": "daily_scan_limit"}
        assert provider.return_value.identify.await_count == 1
        # The second request's lease and cost were given back.
        assert store.count() == 1 and _month_rows(store) == 0
        assert store.leases == {}
        assert store.spend_today()[0] == 0

    def test_retired_enhanced_monthly_key_never_refuses(
        self, client, store, provider, premium
    ):
        """A stored ``quota.premium.enhanced_monthly`` of 0 (the old way to
        refuse every enhanced scan) is ignored: no 402."""
        _settings(**{"quota.premium.enhanced_monthly": 0})
        _finds_books(provider)
        resp = _enhanced(client)
        assert resp.status_code == 200
        provider.return_value.identify.assert_awaited_once()

    def test_zero_daily_allowance_refuses_before_the_provider(
        self, client, store, provider, premium
    ):
        _settings(**{"quota.premium.found_daily": 0})
        resp = _enhanced(client)
        assert resp.json()["detail"] == "daily_scan_limit"
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
        assert _month_rows(store) == 0
        assert store.count() == 0
        assert store.zero_results == {}

    def test_provider_error_keeps_the_unit_and_writes_no_row(
        self, client, store, provider, premium
    ):
        _settings()
        provider.return_value.identify = AsyncMock(side_effect=RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            _enhanced(client)
        # Kept (the provider may have been paid).
        assert store.count() == 1
        assert _month_rows(store) == 0
        assert store.zero_results == {}


# ---------------------------------------------------------------------------
# Charge decision and zero-result tracking
# ---------------------------------------------------------------------------


class TestZeroResultRecord:
    def test_zero_results_are_recorded_up_to_the_threshold(
        self, client, store, provider, premium
    ):
        """#582: every zero-result enhanced scan uses the daily unit (there
        is no enhanced unit to give back); below the threshold a row is
        recorded (the abuse signal), at it nothing more is written."""
        _settings(**{"scan.enhanced.zero_result_threshold": 3})
        for n in range(1, 4):
            assert _enhanced(client).status_code == 200
            assert store.count() == n
            assert len(store.zero_results[USER.id]) == n
        resp = _enhanced(client)
        assert resp.status_code == 200
        assert len(store.zero_results[USER.id]) == 3
        assert resp.json()["enhanced_scans_remaining"] == 26
        assert store.count() == 4
        assert _month_rows(store) == 0

    def test_books_found_after_threshold_are_charged_as_usual(
        self, client, store, provider, premium
    ):
        _settings(**{"scan.enhanced.zero_result_threshold": 1})
        _enhanced(client)
        _finds_books(provider)
        _enhanced(client)
        assert store.count() == 2
        assert len(store.zero_results[USER.id]) == 1

    def test_threshold_zero_charge_records_nothing(
        self, client, store, provider, premium
    ):
        _settings(**{"scan.enhanced.zero_result_threshold": 0})
        _enhanced(client)
        _enhanced(client)
        assert store.count() == 2
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
        assert len(store.zero_results[USER.id]) == 1  # USER's own count


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
        assert store.count() == 2
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
        "tier": Tier.PREMIUM,
        "allowance": quota.Allowance(30),
        "enhanced": True,
        "zero_result_threshold": 3,
        "zero_result_window_hours": 24,
        "zero_result_action": "charge",
    }
    base.update(kw)
    return quota.Reservation(**base)


class TestSettleGate:
    @pytest.fixture
    def record(self):
        record = AsyncMock(return_value=quota.ZeroResultRecorded(1))
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

    @pytest.mark.parametrize(
        "recorded,sent",
        [
            (quota.ZeroResultRecorded(2), False),
            (quota.ZeroResultRecorded(3, warn=True), True),
            # At the threshold, but a warned row is still in the window.
            (quota.ZeroResultRecorded(3, warn=False), False),
            (None, False),
        ],
    )
    async def test_warning_only_when_the_row_claimed_it(self, record, recorded, sent):
        record.return_value = recorded
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
        record.return_value = quota.ZeroResultRecorded(3, warn=True)

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

    async def test_no_warning_when_the_settle_timed_out(self, record):
        """The settle recorded (and claimed) the warning, then the bound cut
        the lease delete off before the commit: the claim may not be
        committed, so nothing is sent."""
        record.return_value = quota.ZeroResultRecorded(3, warn=True)

        async def hung(db, uid, lease_id):
            await anyio.sleep(5)

        with (
            patch.object(quota, "CLEANUP_TIMEOUT_SECONDS", 0.05),
            patch.object(quota, "release_lease", hung),
            patch.object(quota, "send_zero_result_warning") as warn,
        ):
            # No ledger row: the timed-out outcome reaches the warning check.
            await quota.release(
                self._db(), _reservation(), None, None, quota.ZeroResult(None)
            )
        record.assert_awaited_once()
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
        # #565: the client IP header is stripped from every event.
        assert "cf-connecting-ip" not in later["request"]["headers"]
        assert later["request"]["headers"]["user-agent"]
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
    async def test_usage_is_the_day_count_and_daily_limit_only(self, premium):
        """#582: no monthly or enhanced monthly count; one read of today's
        day row."""
        db = AsyncMock()
        db.execute.return_value = MagicMock(scalar_one_or_none=lambda: 4)
        usage = await quota.usage_for(
            db,
            user=USER,
            feature=quota.SCAN_QUOTA_FEATURE,
            ai=ai_settings.AISettings({"quota.premium.found_daily": 7}),
        )
        # #577: the found row, then the no-match row (the fake returns 4 for
        # both reads).
        assert usage == quota.Usage(
            used_today=4, daily_limit=7, no_match_used_today=4, no_match_daily_limit=5
        )
        sql = str(db.execute.await_args.args[0].compile(dialect=postgresql.dialect()))
        assert "ai_quota.window_kind" in sql
        assert db.execute.await_args.args[0].compile().params["window_kind_1"] == "day"

    def test_endpoint_returns_null_monthly_fields(self, client, premium):
        with patch.object(
            quota, "usage_for", AsyncMock(return_value=quota.Usage(1, 30))
        ):
            body = client.get("/scan/usage").json()
        assert body == {
            "used_today": 1,
            "daily_limit": 30,
            # #577: the found counter again, and the no-match counter.
            "found_used_today": 1,
            "found_daily_limit": 30,
            "no_match_used_today": 0,
            "no_match_daily_limit": 0,
        }


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


# ---------------------------------------------------------------------------
# Reserve: enhanced give-backs (#533 review)
# ---------------------------------------------------------------------------


class TestReserveEnhancedGiveBack:
    async def _reserve(self):
        return await quota.reserve(
            AsyncMock(),
            user=USER,
            feature=quota.SCAN_QUOTA_FEATURE,
            timeout_s=1,
            ai=ai_settings.AISettings({"quota.premium.found_daily": 5}),
            est_cost_micros=10,
            enhanced=True,
        )

    async def test_lost_lease_after_the_daily_unit_gives_it_back(self, store, premium):
        with (
            patch.object(quota, "renew_lease", AsyncMock(return_value=False)),
            pytest.raises(quota.ScanInProgress),
        ):
            await self._reserve()
        assert store.count() == 0 and _month_rows(store) == 0
        assert store.leases == {}

    async def test_cancellation_after_the_daily_unit_gives_it_back(
        self, store, premium
    ):
        with (
            patch.object(
                quota, "renew_lease", AsyncMock(side_effect=asyncio.CancelledError())
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await self._reserve()
        assert store.count() == 0 and _month_rows(store) == 0
        assert store.leases == {}

    @pytest.mark.parametrize("path", ["refund", "reserve"])
    async def test_enhanced_refund_retried_after_a_lost_ack_gives_back_once(
        self, store, premium, path
    ):
        """A refund of an enhanced reservation's unit (since #582 the daily
        unit, the only one it takes) that commits and then loses its
        acknowledgement is retried (#590: the give-back is gated on the lease
        row's flag, so a retry is safe), and the retry gives nothing back:
        exactly one unit returns."""
        key = (USER.id, quota.SCAN_QUOTA_FEATURE, "day", quota.utc_today())
        store.rows[key] = 1  # an earlier scan today
        real = store.give_back_unit
        enhanced_calls = []

        async def ack_lost(db, user_id, lease_id, flag, feature, kind, start):
            outcome = await real(
                db, user_id, lease_id, flag, feature, kind, start
            )  # "committed"
            enhanced_calls.append(outcome)
            if len(enhanced_calls) == 1:
                raise ConnectionError("acknowledgement lost")
            return outcome

        with patch.object(quota, "give_back_unit", ack_lost):
            if path == "refund":
                reservation = await self._reserve()
                assert store.rows[key] == 2
                await quota.refund(AsyncMock(), reservation)
            else:
                with (
                    patch.object(quota, "renew_lease", AsyncMock(return_value=False)),
                    pytest.raises(quota.ScanInProgress),
                ):
                    await self._reserve()
        # Retried; the gate gives nothing back the second time.
        assert enhanced_calls == [quota.UNIT_GIVEN_BACK, quota.UNIT_NOT_TAKEN]
        assert store.rows[key] == 1  # exactly one unit given back

    async def test_shared_refunds_are_retried_after_a_failure(self, store, premium):
        """The daily refund is gated on the lease row's flag (#590), so a failed attempt is retried (#567 had made them one-shot
        while nothing gated them) and the unit comes back."""
        reservation = await self._reserve()
        assert store.count() == 1
        real = store.give_back_unit
        calls = []

        async def flaky(db, user_id, lease_id, flag, feature, kind, start):
            calls.append((feature, kind))
            if (
                feature == quota.SCAN_QUOTA_FEATURE
                and kind == "day"
                and len(calls) == 1
            ):
                raise ConnectionError("blip")
            return await real(db, user_id, lease_id, flag, feature, kind, start)

        with patch.object(quota, "give_back_unit", flaky):
            await quota.refund(AsyncMock(), reservation)
        assert calls.count((quota.SCAN_QUOTA_FEATURE, "day")) == 2
        assert store.count() == 0
        assert store.count(kind="month") == 0


class TestWarningClaim:
    """record_zero_result claims the warning (warned = true on the new row)
    only when the row reaches the threshold and no warned row of the user is
    inside the window."""

    def _db(self):
        db = AsyncMock()
        nested = MagicMock()
        nested.__aenter__ = AsyncMock(return_value=None)
        nested.__aexit__ = AsyncMock(return_value=False)
        db.begin_nested = MagicMock(return_value=nested)
        # A result row for every query: the lease lock finds our lease.
        db.execute = AsyncMock(return_value=MagicMock())
        return db

    @staticmethod
    def _inserted_warned(db) -> bool:
        stmt = db.execute.await_args_list[-1].args[0]
        return stmt.compile().params["warned"]

    @pytest.mark.parametrize(
        "count_before,warned_in_window,warn",
        [
            (1, False, False),  # new count 2 < 3
            (2, False, True),  # reaches 3, nothing warned in window: claim
            (2, True, False),  # reaches 3, but a warned row is still in window
        ],
    )
    async def test_claim(self, count_before, warned_in_window, warn):
        db = self._db()
        checked = AsyncMock(return_value=warned_in_window)
        with (
            patch.object(
                quota, "zero_result_count", AsyncMock(return_value=count_before)
            ),
            patch.object(quota, "warned_in_window", checked),
        ):
            recorded = await quota.record_zero_result(db, _reservation(), None)
        assert recorded == quota.ZeroResultRecorded(count_before + 1, warn)
        assert self._inserted_warned(db) is warn

    async def test_sliding_window_abuser_is_warned_once_per_window(self):
        """Threshold 3; rows age out one by one so every new row brings the
        count back to 3. Only the first claims the warning while its row is
        in the window."""
        warned_rows: list[bool] = []
        db = self._db()

        async def any_warned(_db, _uid, _hours):
            return any(warned_rows)

        with (
            patch.object(quota, "zero_result_count", AsyncMock(return_value=2)),
            patch.object(quota, "warned_in_window", any_warned),
        ):
            for _ in range(4):
                recorded = await quota.record_zero_result(db, _reservation(), None)
                warned_rows.append(recorded.warn)
        assert warned_rows == [True, False, False, False]


class TestLeaseSerialisation:
    """The decision runs only while the request still holds its lease (the
    lease row is locked FOR UPDATE until the settle commits)."""

    async def test_without_the_lease_it_is_charged_with_no_row(self):
        db = TestWarningClaim()._db()
        count = AsyncMock(return_value=0)
        with (
            patch.object(quota, "hold_lease_lock", AsyncMock(return_value=False)),
            patch.object(quota, "zero_result_count", count),
        ):
            assert await quota.record_zero_result(db, _reservation(), None) is None
        count.assert_not_awaited()
        db.execute.assert_not_awaited()  # no refund, no row

    async def test_lock_query_is_for_update_on_our_lease(self):
        db = AsyncMock()
        db.execute.return_value = MagicMock(scalar_one_or_none=lambda: None)
        lease = uuid.uuid4()
        assert await quota.hold_lease_lock(db, USER.id, lease) is False
        sql = str(db.execute.await_args.args[0].compile(dialect=postgresql.dialect()))
        assert "FROM ai_inflight" in sql and "FOR UPDATE" in sql
        assert "lease_id" in sql.split("WHERE", 1)[1]

    async def test_settle_after_the_lease_was_taken_over_is_charged(self, store):
        """A slow request whose lease expired and was taken over: its settle
        keeps the unit, writes no row and sends no warning."""
        reservation = _reservation(zero_result_threshold=1)
        key = (USER.id, quota.SCAN_QUOTA_FEATURE, "day", reservation.day)
        store.rows[key] = 1
        store.leases[USER.id] = uuid.uuid4()  # another request holds it now
        db = AsyncMock()
        db.execute = AsyncMock(return_value=MagicMock())
        with patch.object(quota, "send_zero_result_warning") as warn:
            await quota.release(
                db, reservation, MagicMock(), None, quota.ZeroResult(None)
            )
        assert store.rows[key] == 1
        assert store.zero_results.get(USER.id, []) == []
        warn.assert_not_called()

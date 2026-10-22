"""POST /scan kill switches and quota checks — provider mocked, quota store in
memory (the ai_quota / ai_inflight primitives of app/ai/quota.py, #528)."""

import asyncio
import contextlib
import io
import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError
from starlette.datastructures import Headers
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.ai import quota
from app.ai.providers.base import ProviderResult
from app.ai.registry import FEATURES
from app.api import scan as scan_module
from app.core.config import Settings
from app.main import app
from app.models.user import User
from app.services import scan_quota
from app.services.book_identifier import BookCandidate, ScanUnavailableError
from tests.unit._quota_fake import FakeQuotaStore

USER = User(
    id=uuid.UUID("00000000-0000-0000-0000-0000000005a9"),
    email="caps@example.com",
    enhanced_scan_credits=3,
)


def _real_jpeg() -> bytes:
    # A real, decodable JPEG: since #529 an undecodable upload is rejected
    # with 415 image_undecodable before the reservation.
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), (200, 30, 30)).save(buf, format="JPEG")
    return buf.getvalue()


_JPEG = _real_jpeg()


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
        yield AsyncMock()

    app.dependency_overrides[get_current_user] = lambda: USER
    app.dependency_overrides[get_db] = _fake_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def provider():
    """Mock the vision provider and the downstream lookups."""
    with (
        patch("app.ai.gateway.OpenAIVisionProvider") as id_cls,
        patch("app.api.scan.EnrichmentService") as enrich_cls,
        patch("app.api.scan.DeduplicationService") as dedup_cls,
    ):
        id_cls.return_value.identify = AsyncMock(return_value=ProviderResult([]))
        enrich_cls.return_value.enrich = AsyncMock(return_value=[])
        dedup_cls.return_value.check = AsyncMock(return_value=[])
        yield id_cls


def _set(**values):
    return patch.multiple(scan_module.settings, **values)


class TestKillSwitches:
    def test_scan_disabled_returns_503_before_body_turnstile_or_provider(
        self, client, store, provider
    ):
        with (
            _set(scan_enabled=False, turnstile_required=True),
            patch.object(StarletteUploadFile, "read", new=AsyncMock()) as read,
            patch("app.api.scan._verify_turnstile", new=AsyncMock()) as turnstile,
        ):
            resp = client.post(
                "/scan", files=_file(), data={"cf-turnstile-response": "t"}
            )

        assert resp.status_code == 503
        assert resp.json() == {"detail": "scan_disabled"}
        read.assert_not_awaited()
        turnstile.assert_not_awaited()
        provider.assert_not_called()
        assert store.rows == {}

    def test_read_probe_sees_body_read_when_enabled(self, client, store, provider):
        """Positive control for the read assertions above."""
        with patch.object(
            StarletteUploadFile, "read", new=AsyncMock(return_value=_JPEG)
        ) as read:
            client.post("/scan", files=_file())
        read.assert_awaited()

    def test_scan_disabled_applies_to_enhanced_too(self, client, store, provider):
        with _set(scan_enabled=False):
            resp = client.post("/scan", files=_file(), data={"enhanced": "true"})
        assert resp.json()["detail"] == "scan_disabled"

    def test_enhanced_disabled_rejects_enhanced_scan(self, client, store, provider):
        with (
            _set(scan_enhanced_enabled=False),
            patch.object(StarletteUploadFile, "read", new=AsyncMock()) as read,
        ):
            resp = client.post("/scan", files=_file(), data={"enhanced": "true"})

        assert resp.status_code == 503
        assert resp.json() == {"detail": "enhanced_scan_disabled"}
        read.assert_not_awaited()
        provider.assert_not_called()
        assert store.rows == {}

    def test_enhanced_disabled_leaves_standard_scan_working(
        self, client, store, provider
    ):
        with _set(scan_enhanced_enabled=False):
            resp = client.post("/scan", files=_file())
        assert resp.status_code == 200
        provider.assert_called_once_with(FEATURES["scan.standard"])


class TestPerUserDailyLimit:
    def test_allows_limit_then_429_with_retry_after(self, client, store, provider):
        # Since #528 the per-user daily cap is the ai_settings key
        # quota.free.daily (seeded from SCAN_FREE_DAILY_LIMIT).
        with _set(scan_free_daily_limit=3):
            codes = [client.post("/scan", files=_file()).status_code for _ in range(3)]
            resp = client.post("/scan", files=_file())

        assert codes == [200, 200, 200]
        assert resp.status_code == 429
        assert resp.json() == {"detail": "daily_scan_limit"}
        retry_after = int(resp.headers["Retry-After"])
        assert 1 <= retry_after <= 86_400
        # The rejected request never reached the provider.
        assert provider.return_value.identify.await_count == 3
        assert store.count() == 3

    def test_empty_result_still_counts(self, client, store, provider):
        provider.return_value.identify = AsyncMock(return_value=ProviderResult([]))
        resp = client.post("/scan", files=_file())
        assert resp.status_code == 200
        assert resp.json()["books"] == []
        assert store.count() == 1

    def test_enhanced_premium_check_runs_before_reservation(
        self, client, store, provider
    ):
        # #533 replaced the credit check: a Free user's enhanced scan is
        # refused before anything is reserved.
        free = User(id=uuid.uuid4(), email="free@example.com")
        from app.auth.dependencies import get_current_user

        app.dependency_overrides[get_current_user] = lambda: free
        resp = client.post("/scan", files=_file(), data={"enhanced": "true"})
        assert resp.status_code == 403
        assert resp.json() == {"detail": "enhanced_requires_premium"}
        assert store.rows == {}
        assert store.leases == {}


class TestGlobalDailyLimit:
    def test_global_limit_returns_503_scan_capacity(self, client, store, provider):
        store.extra_global = 50
        with _set(scan_global_daily_limit=50):
            resp = client.post("/scan", files=_file())

        assert resp.status_code == 503
        assert resp.json() == {"detail": "scan_capacity"}
        provider.return_value.identify.assert_not_awaited()
        assert store.count() == 0

    def test_under_global_limit_proceeds(self, client, store, provider):
        store.extra_global = 49
        with _set(scan_global_daily_limit=50):
            resp = client.post("/scan", files=_file())
        assert resp.status_code == 200


class TestRefund:
    def test_provider_unavailable_refunds_and_returns_503(
        self, client, store, provider
    ):
        provider.return_value.identify = AsyncMock(
            side_effect=ScanUnavailableError("timeout")
        )
        resp = client.post("/scan", files=_file())
        assert resp.status_code == 503
        assert resp.json()["detail"] == "scan_unavailable"
        assert store.count() == 0

    def test_connection_never_established_refunds_and_returns_503(
        self, client, store, provider
    ):
        """A ConnectError means the provider was never reached, so nothing billed."""
        import httpx

        provider.return_value.identify = AsyncMock(
            side_effect=httpx.ConnectError("refused")
        )
        resp = client.post("/scan", files=_file())
        assert resp.status_code == 503
        assert resp.json()["detail"] == "scan_unavailable"
        assert store.count() == 0

    def test_unexpected_provider_error_is_not_refunded(self, store, provider):
        """Other errors don't refund: the provider may have been paid."""
        from app.auth.dependencies import get_current_user
        from app.core.database import get_db

        async def _fake_db():
            yield AsyncMock()

        app.dependency_overrides[get_current_user] = lambda: USER
        app.dependency_overrides[get_db] = _fake_db
        provider.return_value.identify = AsyncMock(side_effect=RuntimeError("boom"))
        try:
            resp = TestClient(app, raise_server_exceptions=False).post(
                "/scan", files=_file()
            )
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code == 500
        assert store.count() == 1

    async def test_cancelled_provider_call_is_not_refunded(self, store, provider):
        # Called directly (below the rate-limit wrapper) so the CancelledError
        # reaches the test instead of being turned into a transport error.
        provider.return_value.identify = AsyncMock(side_effect=asyncio.CancelledError())
        upload = StarletteUploadFile(
            file=io.BytesIO(_JPEG),
            filename="scan.jpg",
            headers=Headers({"content-type": "image/jpeg"}),
        )
        with (
            _set(turnstile_required=False),
            pytest.raises(asyncio.CancelledError),
        ):
            await scan_module.scan.__wrapped__(
                request=MagicMock(),
                file=upload,
                current_user=USER,
                db=AsyncMock(),
                cf_turnstile_response=None,
                enhanced=False,
            )
        provider.return_value.identify.assert_awaited_once()
        assert store.count() == 1

    def test_successful_scan_is_not_refunded(self, client, store, provider):
        provider.return_value.identify = AsyncMock(
            return_value=ProviderResult(
                [BookCandidate(title="Dune", author="F", confidence=0.9)]
            )
        )
        client.post("/scan", files=_file())
        assert store.count() == 1

    def test_refund_failure_does_not_mask_provider_error(self, client, provider):
        provider.return_value.identify = AsyncMock(
            side_effect=ScanUnavailableError("timeout")
        )
        with (
            patch.object(quota, "global_units_today", AsyncMock(return_value=0)),
            patch.object(quota, "acquire_lease", AsyncMock(return_value=uuid.uuid4())),
            patch.object(quota, "take_unit", AsyncMock(return_value=1)),
            patch.object(
                quota, "give_back_unit", AsyncMock(side_effect=OSError("db down"))
            ),
            patch.object(quota, "release_lease", AsyncMock(return_value=True)),
        ):
            resp = client.post("/scan", files=_file())
        assert resp.status_code == 503
        assert resp.json()["detail"] == "scan_unavailable"


class TestFailClosed:
    def test_reservation_error_blocks_provider_and_returns_5xx(self, provider):
        from app.auth.dependencies import get_current_user
        from app.core.database import get_db

        async def _fake_db():
            yield AsyncMock()

        app.dependency_overrides[get_current_user] = lambda: USER
        app.dependency_overrides[get_db] = _fake_db
        try:
            with (
                patch.object(quota, "global_units_today", AsyncMock(return_value=0)),
                patch.object(
                    quota, "acquire_lease", AsyncMock(return_value=uuid.uuid4())
                ),
                patch.object(quota, "take_unit", AsyncMock(side_effect=OSError("db"))),
                patch.object(quota, "release_lease", AsyncMock(return_value=True)),
            ):
                resp = TestClient(app, raise_server_exceptions=False).post(
                    "/scan", files=_file()
                )
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code >= 500
        provider.assert_not_called()
        provider.return_value.identify.assert_not_awaited()

    def test_global_check_error_blocks_provider_and_returns_5xx(self, provider):
        from app.auth.dependencies import get_current_user
        from app.core.database import get_db

        async def _fake_db():
            yield AsyncMock()

        app.dependency_overrides[get_current_user] = lambda: USER
        app.dependency_overrides[get_db] = _fake_db
        try:
            with patch.object(
                quota, "global_units_today", AsyncMock(side_effect=OSError("db"))
            ):
                resp = TestClient(app, raise_server_exceptions=False).post(
                    "/scan", files=_file()
                )
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code >= 500
        provider.return_value.identify.assert_not_awaited()


class TestRequestSession:
    def test_quota_calls_use_the_request_session(self, client, store, provider):
        """Reservation and refund run on the request's session (no second
        pool checkout)."""
        seen = []
        request_db = AsyncMock()

        async def _db():
            yield request_db

        from app.core.database import get_db

        app.dependency_overrides[get_db] = _db
        provider.return_value.identify = AsyncMock(
            side_effect=ScanUnavailableError("timeout")
        )

        def _record(fn):
            async def wrapper(db, *args):
                seen.append(db)
                return await fn(db, *args)

            return wrapper

        names = (
            "global_units_today",
            "acquire_lease",
            "renew_lease",
            "release_lease",
            "take_unit",
            "give_back_unit",
        )
        with contextlib.ExitStack() as stack:
            for name in names:
                stack.enter_context(
                    patch.object(quota, name, _record(getattr(store, name)))
                )
            other_sessions = stack.enter_context(
                patch("app.core.database.AsyncSessionLocal")
            )
            resp = client.post("/scan", files=_file())

        assert resp.status_code == 503
        # global, lease, daily, monthly, lease renewal, daily refund,
        # monthly refund, release
        assert len(seen) == 8
        assert all(db is request_db for db in seen)
        other_sessions.assert_not_called()


class TestQuotaHelpers:
    def test_seconds_until_utc_midnight(self):
        now = datetime(2026, 10, 21, 23, 59, 0, tzinfo=UTC)
        assert scan_quota.seconds_until_utc_midnight(now) == 60
        start = datetime(2026, 10, 21, 0, 0, 0, tzinfo=UTC)
        assert scan_quota.seconds_until_utc_midnight(start) == 86_400

    def test_seconds_until_midnight_is_at_least_one(self):
        now = datetime(2026, 10, 21, 23, 59, 59, 999_999, tzinfo=UTC)
        assert scan_quota.seconds_until_utc_midnight(now) == 1

    def test_utc_today_uses_utc(self):
        from datetime import timedelta, timezone

        late_pacific = datetime(
            2026, 10, 21, 20, 0, tzinfo=timezone(timedelta(hours=-7))
        )
        assert scan_quota.utc_today(late_pacific) == date(2026, 10, 22)

    async def test_purge_deletes_in_bounded_batches(self):
        results = [
            MagicMock(rowcount=scan_quota.PURGE_BATCH_SIZE),
            MagicMock(rowcount=7),
        ]
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=results)
        total = await scan_quota.purge_old_scan_usage(db, today=date(2026, 10, 21))
        assert total == scan_quota.PURGE_BATCH_SIZE + 7
        assert db.execute.await_count == 2
        assert "scan_usage_daily" in str(db.execute.call_args[0][0])

    async def test_purge_stops_at_max_batches(self):
        db = AsyncMock()
        db.execute = AsyncMock(
            return_value=MagicMock(rowcount=scan_quota.PURGE_BATCH_SIZE)
        )
        await scan_quota.purge_old_scan_usage(db)
        assert db.execute.await_count == scan_quota.PURGE_MAX_BATCHES


_DB = "postgresql+asyncpg://u:p@localhost/db"


class TestSettingsBounds:
    def test_defaults(self):
        s = Settings(database_url=_DB)
        assert s.scan_enabled is True
        assert s.scan_enhanced_enabled is True
        assert s.scan_daily_limit_per_user == 20
        assert s.scan_global_daily_limit == 2000

    @pytest.mark.parametrize("value", [0, -1, 1001])
    def test_per_user_limit_out_of_bounds_rejected(self, value):
        with pytest.raises(ValidationError):
            Settings(database_url=_DB, scan_daily_limit_per_user=value)

    @pytest.mark.parametrize("value", [0, 1_000_001])
    def test_global_limit_out_of_bounds_rejected(self, value):
        with pytest.raises(ValidationError):
            Settings(database_url=_DB, scan_global_daily_limit=value)

    @pytest.mark.parametrize(
        "field,value",
        [("scan_daily_limit_per_user", 1000), ("scan_global_daily_limit", 1)],
    )
    def test_bounds_inclusive(self, field, value):
        assert getattr(Settings(database_url=_DB, **{field: value}), field) == value

    def test_read_from_env(self, monkeypatch):
        monkeypatch.setenv("SCAN_ENABLED", "false")
        monkeypatch.setenv("SCAN_DAILY_LIMIT_PER_USER", "5")
        s = Settings(database_url=_DB)
        assert s.scan_enabled is False
        assert s.scan_daily_limit_per_user == 5

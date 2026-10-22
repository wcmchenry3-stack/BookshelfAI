"""POST /scan quota refusals and GET /scan/usage (#528) — provider mocked,
ai_quota / ai_inflight faked in memory."""

import asyncio
import dataclasses
import io
import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from starlette.datastructures import Headers
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.ai import entitlements, quota
from app.ai import settings as ai_settings
from app.ai.entitlements import Tier
from app.ai.providers.base import ProviderResult
from app.api import scan as scan_module
from app.main import app
from app.models.user import User
from app.services.book_identifier import ScanUnavailableError
from tests.unit._quota_fake import FakeQuotaStore

USER = User(
    id=uuid.UUID("00000000-0000-0000-0000-000000000528"),
    email="quota@example.com",
)


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), (30, 30, 200)).save(buf, format="JPEG")
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


@pytest.fixture
def premium(monkeypatch):
    """Enhanced scans are Premium-only (#533)."""
    monkeypatch.setattr(entitlements, "tier_for", AsyncMock(return_value=Tier.PREMIUM))


def _today_month():
    today = quota.utc_today()
    return today, quota.month_start(today)


class TestRefusalResponses:
    def test_daily_limit_429_with_retry_after_to_utc_midnight(
        self, client, store, provider
    ):
        today, _ = _today_month()
        store.rows[(USER.id, quota.SCAN_QUOTA_FEATURE, "day", today)] = 5
        resp = client.post("/scan", files=_file())
        assert resp.status_code == 429
        assert resp.json() == {"detail": "daily_scan_limit"}
        assert 1 <= int(resp.headers["Retry-After"]) <= 86_400
        provider.assert_not_called()
        assert store.leases == {}  # lease released

    def test_monthly_limit_402_with_limit_used_and_period_end(
        self, client, store, provider
    ):
        today, month = _today_month()
        store.rows[(USER.id, quota.SCAN_QUOTA_FEATURE, "month", month)] = 5
        resp = client.post("/scan", files=_file())
        assert resp.status_code == 402
        assert resp.json() == {
            "detail": "scan_limit_reached",
            "limit": 5,
            "used": 5,
            "period_end": quota.next_month_start(today).isoformat(),
        }
        assert "Retry-After" not in resp.headers
        provider.assert_not_called()
        assert store.leases == {}
        assert store.count() == 0  # the daily reservation was given back

    def test_scan_in_progress_429_retry_after_2(self, client, store, provider):
        store.leases[USER.id] = held = uuid.uuid4()
        resp = client.post("/scan", files=_file())
        assert resp.status_code == 429
        assert resp.json() == {"detail": "scan_in_progress"}
        assert resp.headers["Retry-After"] == "2"
        provider.assert_not_called()
        assert store.leases == {USER.id: held}
        assert store.rows == {}

    def test_global_capacity_503_takes_no_lease(self, client, store, provider):
        store.extra_global = 10
        with patch.object(scan_module.settings, "scan_global_daily_limit", 10):
            resp = client.post("/scan", files=_file())
        assert resp.status_code == 503
        assert resp.json() == {"detail": "scan_capacity"}
        assert store.releases == 0 and store.leases == {}

    def test_global_cap_comes_from_ai_settings(self, client, store, provider):
        ai_settings.reset_cache({"scan.global_daily_limit": 3})
        store.extra_global = 3
        resp = client.post("/scan", files=_file())
        assert resp.json() == {"detail": "scan_capacity"}

    def test_allowance_comes_from_ai_settings(self, client, store, provider):
        ai_settings.reset_cache({"quota.free.daily": 1})
        assert client.post("/scan", files=_file()).status_code == 200
        resp = client.post("/scan", files=_file())
        assert resp.json() == {"detail": "daily_scan_limit"}

    def test_402_codes_differ_for_enhanced_and_monthly_limit(
        self, client, store, provider, premium
    ):
        """Both refusals are 402; the client keys on the detail string. (#533
        replaced no_enhanced_credits with enhanced_limit_reached.)"""
        today, month = _today_month()
        ai_settings.reset_cache({"quota.premium.enhanced_monthly": 2})
        store.rows[(USER.id, quota.ENHANCED_QUOTA_FEATURE, "month", month)] = 2
        resp = client.post("/scan", files=_file(), data={"enhanced": "true"})
        assert resp.status_code == 402
        assert resp.json() == {
            "detail": "enhanced_limit_reached",
            "limit": 2,
            "used": 2,
            "period_end": quota.next_month_start(today).isoformat(),
        }
        provider.assert_not_called()
        assert store.leases == {}
        # The shared units taken before it were given back.
        assert store.count() == 0 and store.count(kind="month") == 0


class TestOrderAndSettle:
    def test_lease_released_after_success_and_units_kept(self, client, store, provider):
        assert client.post("/scan", files=_file()).status_code == 200
        assert store.leases == {}
        assert store.count() == 1
        assert store.count(kind="month") == 1

    def test_provider_unavailable_refunds_both_and_releases(
        self, client, store, provider
    ):
        provider.return_value.identify = AsyncMock(side_effect=ScanUnavailableError())
        resp = client.post("/scan", files=_file())
        assert resp.json() == {"detail": "scan_unavailable"}
        assert store.count() == 0 and store.count(kind="month") == 0
        assert store.leases == {}

    @pytest.mark.parametrize("enhanced", [False, True])
    def test_provider_past_its_deadline_refunds_and_503s(
        self, client, store, provider, enhanced, premium
    ):
        """The gateway's overall deadline (spec timeout_s, here 0.05 s instead
        of 45 s) turns a hung provider into scan_unavailable, which refunds."""

        async def hung(_image):
            await anyio.sleep(2)  # far past 0.05 s; short if the bound is missing
            return ProviderResult([])

        def fast_spec(self, spec):
            fast = dataclasses.replace(spec)
            object.__setattr__(fast, "timeout_s", 0.05)
            return fast

        provider.return_value.identify = hung
        with patch.object(ai_settings.AISettings, "spec_for", fast_spec):
            resp = client.post(
                "/scan", files=_file(), data={"enhanced": "true"} if enhanced else {}
            )
        assert resp.status_code == 503
        assert resp.json() == {"detail": "scan_unavailable"}
        assert store.count() == 0 and store.count(kind="month") == 0
        assert store.leases == {}

    def test_enhanced_scans_use_the_shared_scan_counter(
        self, client, store, provider, premium
    ):
        assert (
            client.post("/scan", files=_file(), data={"enhanced": "true"}).status_code
            == 200
        )
        assert client.post("/scan", files=_file()).status_code == 200
        assert store.count() == 2
        assert store.count(kind="month") == 2
        # One shared key for every scan (OD-1), plus the enhanced allowance's
        # own month key (#533); nothing else.
        assert {feature for (_, feature, _, _) in store.rows} == {
            quota.SCAN_QUOTA_FEATURE,
            quota.ENHANCED_QUOTA_FEATURE,
        }

    @pytest.mark.parametrize("sixth", ["standard", "enhanced"])
    def test_three_standard_and_two_enhanced_exhaust_a_daily_limit_of_5(
        self, client, store, provider, sixth, premium
    ):
        ai_settings.reset_cache({"quota.premium.daily": 5})
        enhanced = {"enhanced": "true"}
        for data in ({}, {}, {}, enhanced, enhanced):
            assert client.post("/scan", files=_file(), data=data).status_code == 200
        assert store.count() == 5
        resp = client.post(
            "/scan", files=_file(), data=enhanced if sixth == "enhanced" else {}
        )
        assert resp.status_code == 429
        assert resp.json() == {"detail": "daily_scan_limit"}
        assert store.count() == 5
        assert provider.return_value.identify.await_count == 5

    def test_enhanced_refund_gives_back_the_shared_units(
        self, client, store, provider, premium
    ):
        provider.return_value.identify = AsyncMock(side_effect=ScanUnavailableError())
        resp = client.post("/scan", files=_file(), data={"enhanced": "true"})
        assert resp.json() == {"detail": "scan_unavailable"}
        assert store.count() == 0 and store.count(kind="month") == 0
        assert store.rows == {
            (USER.id, quota.SCAN_QUOTA_FEATURE, "day", quota.utc_today()): 0,
            (
                USER.id,
                quota.SCAN_QUOTA_FEATURE,
                "month",
                quota.month_start(quota.utc_today()),
            ): 0,
            # #533: the enhanced unit follows the shared units.
            (
                USER.id,
                quota.ENHANCED_QUOTA_FEATURE,
                "month",
                quota.month_start(quota.utc_today()),
            ): 0,
        }

    def test_disabled_scan_takes_no_lease(self, client, store, provider):
        with patch.object(scan_module.settings, "scan_enabled", False):
            resp = client.post("/scan", files=_file())
        assert resp.json() == {"detail": "scan_disabled"}
        assert store.releases == 0 and store.rows == {}

    def test_undecodable_image_takes_no_lease(self, client, store, provider):
        fake = b"\xff\xd8\xff\xe0" + b"x" * 200
        resp = client.post(
            "/scan", files={"file": ("s.jpg", io.BytesIO(fake), "image/jpeg")}
        )
        assert resp.status_code == 415
        assert store.releases == 0 and store.rows == {}

    async def test_cancelled_provider_call_keeps_units_and_releases_lease(
        self, store, provider
    ):
        provider.return_value.identify = AsyncMock(side_effect=asyncio.CancelledError())
        upload = StarletteUploadFile(
            file=io.BytesIO(_JPEG),
            filename="scan.jpg",
            headers=Headers({"content-type": "image/jpeg"}),
        )
        with (
            patch.object(scan_module.settings, "turnstile_required", False),
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
        assert store.count() == 1
        assert store.leases == {}

    def test_release_failure_does_not_mask_the_result(self, client, store, provider):
        with patch.object(
            quota, "release_lease", AsyncMock(side_effect=OSError("db down"))
        ):
            resp = client.post("/scan", files=_file())
        assert resp.status_code == 200


class TestUsageEndpoint:
    def test_returns_current_usage_and_limits(self, client, store):
        today, month = _today_month()
        store.rows[(USER.id, quota.SCAN_QUOTA_FEATURE, "day", today)] = 2
        store.rows[(USER.id, quota.SCAN_QUOTA_FEATURE, "month", month)] = 4
        with patch.object(
            quota,
            "usage_for",
            AsyncMock(
                return_value=quota.Usage(2, 5, 4, 5, quota.next_month_start(today))
            ),
        ) as usage_for:
            resp = client.get("/scan/usage")
        assert resp.status_code == 200
        assert resp.json() == {
            "used_today": 2,
            "daily_limit": 5,
            "used_month": 4,
            "monthly_limit": 5,
            "period_end": quota.next_month_start(today).isoformat(),
            # #533
            "enhanced_used_month": 0,
            "enhanced_monthly_limit": 0,
        }
        assert usage_for.await_args.kwargs["feature"] == quota.SCAN_QUOTA_FEATURE
        assert usage_for.await_args.kwargs["user"] is USER

    async def test_usage_for_reads_rows_and_tier_limits(self):
        db = AsyncMock()
        db.execute.return_value = MagicMock(
            all=lambda: [
                (quota.SCAN_QUOTA_FEATURE, "day", 2),
                (quota.SCAN_QUOTA_FEATURE, "month", 4),
            ]
        )
        usage = await quota.usage_for(
            db,
            user=USER,
            feature=quota.SCAN_QUOTA_FEATURE,
            ai=ai_settings.AISettings({"quota.free.monthly": 9}),
        )
        assert (usage.used_today, usage.used_month) == (2, 4)
        assert (usage.daily_limit, usage.monthly_limit) == (5, 9)
        assert isinstance(usage.period_end, date)

    def test_ai_settings_unavailable_is_503(self, client):
        with patch(
            "app.api.scan.get_ai_settings",
            AsyncMock(side_effect=ai_settings.AISettingsUnavailableError()),
        ):
            resp = client.get("/scan/usage")
        assert resp.status_code == 503
        assert resp.json() == {"detail": "ai_unavailable"}

    def test_post_not_allowed(self, client):
        assert client.post("/scan/usage").status_code == 405

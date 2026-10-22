"""Cost circuit breaker authority (#531): at the daily ceiling Free scans are
refused before the provider, Premium (within its own caps) still reaches it
(decision #544), and nothing a client sends can change the ceiling, the
thresholds, the tier or the spend.

Run on its own with:  pytest -m security tests/security/test_cost_breaker_authority.py
"""

import ast
import io
import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import sentry_sdk
from fastapi.testclient import TestClient
from PIL import Image

import app as app_package
from app.ai import breaker, entitlements, quota
from app.ai.entitlements import Tier
from app.ai.providers.base import ProviderResult
from app.core.config import settings
from app.core.sentry_context import set_sentry_user
from app.main import app
from app.models.user import User
from tests.unit._quota_fake import FakeQuotaStore
from tests.unit._sentry_capture import captured_app_events, captured_events

pytestmark = pytest.mark.security

APP_ROOT = Path(app_package.__file__).resolve().parent
USER = User(
    id=uuid.UUID("00000000-0000-0000-0000-00000000531a"),
    email="breaker-authority@example.com",
    enhanced_scan_credits=0,
)

# Every name a client might try, as form fields, query params and headers.
_FORGED = {
    "breaker.free_daily_ceiling_micros": "1000000000",
    "free_tier_daily_ceiling_micros": "1000000000",
    "ceiling": "1000000000",
    "ceiling_micros": "1000000000",
    "est_cost_micros": "0",
    "cost_micros": "0",
    "threshold": "100",
    "thresholds": "100,100,100",
    "alerted_mask": "7",
    "settled_micros": "0",
    "reserved_micros": "0",
    "tier": "premium",
    "plan": "premium",
}


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (1, 2, 3)).save(buf, format="JPEG")
    return buf.getvalue()


_JPEG = _jpeg()


@pytest.fixture
def store():
    s = FakeQuotaStore(default_user=USER.id)
    s.spend[quota.utc_today()] = [0, settings.scan_free_daily_ceiling_micros, 0]
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


def _post(client, **kwargs):
    return client.post(
        "/scan",
        files={"file": ("s.jpg", io.BytesIO(_JPEG), "image/jpeg")},
        **kwargs,
    )


class TestAtTheCeiling:
    @pytest.mark.parametrize("enhanced", [False, True])
    def test_free_gets_503_ai_budget_reached_and_provider_never_called(
        self, client, store, provider, enhanced
    ):
        USER.enhanced_scan_credits = 3 if enhanced else 0
        try:
            resp = _post(client, data={"enhanced": "true"} if enhanced else {})
        finally:
            USER.enhanced_scan_credits = 0
        assert resp.status_code == 503
        assert resp.json() == {"detail": "ai_budget_reached"}
        provider.assert_not_called()
        provider.return_value.identify.assert_not_called()
        assert store.leases == {} and store.count() == 0

    def test_premium_within_its_caps_reaches_the_provider(
        self, client, store, provider, monkeypatch
    ):
        monkeypatch.setattr(
            entitlements, "tier_for", AsyncMock(return_value=Tier.PREMIUM)
        )
        resp = _post(client)
        assert resp.status_code == 200
        provider.return_value.identify.assert_awaited_once()

    def test_premium_over_its_own_cap_is_still_refused(
        self, client, store, provider, monkeypatch
    ):
        monkeypatch.setattr(
            entitlements, "tier_for", AsyncMock(return_value=Tier.PREMIUM)
        )
        today = quota.utc_today()
        store.rows[(USER.id, quota.SCAN_QUOTA_FEATURE, "day", today)] = (
            settings.scan_premium_daily_limit
        )
        assert _post(client).json() == {"detail": "daily_scan_limit"}
        provider.return_value.identify.assert_not_called()


class TestClientCannotChangeTheCeiling:
    def test_forged_fields_headers_and_query_do_not_lift_the_ceiling(
        self, client, store, provider
    ):
        resp = _post(
            client,
            data=_FORGED,
            params=_FORGED,
            headers={f"X-{k.replace('.', '-')}": v for k, v in _FORGED.items()},
        )
        assert resp.json() == {"detail": "ai_budget_reached"}
        provider.return_value.identify.assert_not_called()

    def test_forged_values_do_not_reach_the_breaker(self, client, store, provider):
        """With room under the ceiling: the estimate and ceiling the breaker
        sees are the registry's and ai_settings', whatever the client sends."""
        store.spend.clear()
        seen = []
        real = store.spend_reserve

        async def spy(db, day, est, ceiling, *, enforce):
            seen.append((est, ceiling, enforce))
            return await real(db, day, est, ceiling, enforce=enforce)

        with patch.object(breaker, "reserve", spy):
            _post(
                client,
                data=_FORGED,
                params=_FORGED,
                headers={"X-Ceiling": "0", "X-Tier": "premium"},
            )
        assert seen == [(10_000, settings.scan_free_daily_ceiling_micros, True)]

    def test_alert_thresholds_are_fixed_in_code(self):
        assert breaker.ALERT_THRESHOLDS_PCT == (50, 75, 90)
        assert isinstance(breaker.ALERT_THRESHOLDS_PCT, tuple)

    def test_no_route_writes_ai_spend_daily_or_the_ceiling(self):
        """Only the breaker (and the cleanup task through it) touches the
        spend table, and no router mentions the ceiling key."""
        offenders = []
        for path in (APP_ROOT / "api").rglob("*.py"):
            text = path.read_text()
            if "ai_spend_daily" in text or "AISpendDaily" in text:
                offenders.append(str(path))
            if "free_daily_ceiling" in text:
                offenders.append(str(path))
        assert offenders == []

    def test_breaker_does_not_import_providers(self):
        tree = ast.parse((APP_ROOT / "ai" / "breaker.py").read_text())
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert not any(m.startswith("app.ai.providers") for m in imported)


class TestAlertContent:
    def test_alert_event_has_no_user_or_request_context(self):
        """Through a real SDK client: the request's user (set by
        get_current_user) and tags never reach the alert event, and the
        alert's fields are only the fixed tags and extras."""
        day = quota.utc_today()
        with captured_events() as events:
            set_sentry_user(USER.id)
            sentry_sdk.set_tag("endpoint", "/scan")
            sentry_sdk.add_breadcrumb(message=f"user {USER.email}")
            breaker.send_alert(
                day=day,
                pct=90,
                settled_micros=9_000_000,
                reserved_micros=10_000,
                ceiling_micros=10_000_000,
                settlement=breaker.Settlement(
                    600, "scan.standard", "gpt-4o-mini", "ok"
                ),
            )
            sentry_sdk.capture_message("later")
        alert, later = events
        assert "user" not in alert
        assert str(USER.id) not in json.dumps(alert)
        assert USER.email not in json.dumps(alert)
        assert alert["fingerprint"] == ["ai-breaker", day.isoformat(), "90"]
        assert set(alert["tags"]) == {
            "feature",
            "model",
            "status",
            "breaker_threshold_pct",
        }
        assert set(alert["extra"]) == {
            "day",
            "threshold_pct",
            "settled_micros",
            "reserved_micros",
            "ceiling_micros",
        }
        # The request's own later events are unchanged.
        assert later["user"] == {"id": str(USER.id)}
        assert later["tags"]["endpoint"] == "/scan"
        assert not {"feature", "model", "breaker_threshold_pct"} & set(later["tags"])
        assert "fingerprint" not in later

    def test_alert_inside_a_real_request_carries_no_request_data(self):
        """With the app's Sentry setup (Starlette/FastAPI integrations on),
        the integration's event processor attaches the HTTP request (url,
        headers incl. CF-Connecting-IP and User-Agent, body) to every event in
        the request. The spend alert must carry none of it, while a later
        ordinary event in the same request still does."""
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
            breaker.send_alert(
                day=quota.utc_today(),
                pct=50,
                settled_micros=5_000_000,
                reserved_micros=10_000,
                ceiling_micros=10_000_000,
                settlement=breaker.Settlement(
                    600, "scan.standard", "gpt-4o-mini", "ok"
                ),
            )
            sentry_sdk.capture_message("later in the same request")
            return {"ok": True}

        with captured_app_events() as events:
            resp = TestClient(mini).post(
                "/scan",
                headers={
                    "CF-Connecting-IP": "198.51.100.7",
                    "User-Agent": "Probe/1.0 (iPhone)",
                    "Accept-Language": "he-IL",
                },
                data={"enhanced": "true", "note": "shelf by the window"},
            )
        assert resp.status_code == 200
        alert, later = events
        assert alert["fingerprint"][0] == "ai-breaker"
        for key in ("request", "user", "breadcrumbs"):
            assert key not in alert, key
        assert set(alert.get("contexts", {})) <= {"trace", "runtime", "os"}
        text = json.dumps(alert)
        for leak in ("198.51.100.7", "Probe/1.0", "he-IL", "shelf by the window"):
            assert leak not in text
        assert str(USER.id) not in text
        # The request's own later event is unaffected.
        assert later["request"]["headers"]["cf-connecting-ip"] == "198.51.100.7"
        assert later["request"]["headers"]["user-agent"] == "Probe/1.0 (iPhone)"
        assert later["user"] == {"id": str(USER.id)}
        assert later["breadcrumbs"]["values"]
        assert "request_ctx" in later["contexts"]

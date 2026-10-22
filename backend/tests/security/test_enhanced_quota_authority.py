"""Enhanced-scan authority (#533): enhanced scans are Premium-only and bound by
the server's enhanced allowance and zero-result limits, and nothing a client
sends (form fields, query parameters, headers) can change the tier, the
allowance, the remaining count, the threshold, the window or the action. One
user's zero-result rows never affect another's, the zero-result warning
carries no user or request data, and the legacy credit balance is no longer
read or written by the app.

Run on its own with:  pytest -m security tests/security/test_enhanced_quota_authority.py
"""

import ast
import io
import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import app as app_package
from app.ai import entitlements, quota
from app.ai import settings as ai_settings
from app.ai.entitlements import Tier
from app.ai.providers.base import ProviderResult
from app.core.sentry_context import set_sentry_user
from app.main import app
from app.models.user import User
from app.services.book_identifier import BookCandidate
from tests.unit._quota_fake import FakeQuotaStore
from tests.unit._sentry_capture import captured_app_events

pytestmark = pytest.mark.security

APP_ROOT = Path(app_package.__file__).resolve().parent
USER = User(
    id=uuid.UUID("00000000-0000-0000-0000-00000000533a"),
    email="enhanced-authority@example.com",
)
OTHER = uuid.UUID("00000000-0000-0000-0000-00000000533b")

# Every name a client might try, as form fields, query params and headers.
_FORGED = {
    "tier": "premium",
    "plan": "premium",
    "is_premium": "true",
    "subscription": "premium",
    "enhanced_scans_remaining": "1000",
    "enhanced_scan_credits": "1000",
    "enhanced_monthly_limit": "1000",
    "quota.premium.enhanced_monthly": "1000",
    "zero_result_threshold": "20",
    "scan.enhanced.zero_result_threshold": "20",
    "enhanced_zero_result_threshold": "20",
    "zero_result_window_hours": "1",
    "zero_result_action": "charge",
    "enhanced_zero_result_action": "charge",
    "user_id": str(OTHER),
}


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (5, 3, 3)).save(buf, format="JPEG")
    return buf.getvalue()


_JPEG = _jpeg()


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

    def _user():
        set_sentry_user(USER.id)  # as get_current_user does
        return USER

    app.dependency_overrides[get_current_user] = _user
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


def _post(client, forged=True):
    kwargs = {}
    if forged:
        kwargs = {
            "params": _FORGED,
            "headers": {f"X-{k.replace('.', '-')}": v for k, v in _FORGED.items()},
        }
    return client.post(
        "/scan",
        files={"file": ("s.jpg", io.BytesIO(_JPEG), "image/jpeg")},
        data={**(_FORGED if forged else {}), "enhanced": "true"},
        **kwargs,
    )


class TestPremiumOnly:
    @pytest.mark.parametrize("forged", [False, True])
    def test_free_enhanced_refused_before_any_reservation_or_provider(
        self, client, store, provider, forged
    ):
        _settings()
        resp = _post(client, forged=forged)
        assert resp.status_code == 403
        assert resp.json() == {"detail": "enhanced_requires_premium"}
        provider.assert_not_called()
        assert store.rows == {}  # no units
        assert store.leases == {} and store.releases == 0  # no lease
        assert store.spend == {}  # no cost estimate

    def test_tier_for_never_sees_the_request(self, client, store, provider):
        seen = []

        async def spy(db, user):
            seen.append(user)
            return Tier.FREE

        with patch.object(entitlements, "tier_for", spy):
            _post(client)
        assert seen == [USER]


class TestAllowanceAuthority:
    def test_zero_enhanced_allowance_refused_before_the_provider(
        self, client, store, provider, premium
    ):
        _settings(**{"quota.premium.enhanced_monthly": 0})
        resp = _post(client)
        assert resp.status_code == 402
        assert resp.json()["detail"] == "enhanced_limit_reached"
        assert resp.json()["limit"] == 0
        provider.assert_not_called()
        assert store.leases == {}
        assert store.count() == 0 and store.count(kind="month") == 0

    def test_forged_remaining_is_ignored(self, client, store, provider, premium):
        _settings(**{"quota.premium.enhanced_monthly": 5})
        provider.return_value.identify = AsyncMock(
            return_value=ProviderResult(
                [BookCandidate(title="Dune", author="F", confidence=0.9)]
            )
        )
        from app.schemas.book import EnrichedBook

        with patch("app.api.scan.EnrichmentService") as enrich:
            enrich.return_value.enrich = AsyncMock(
                return_value=[EnrichedBook(title="Dune", author="F", confidence=0.9)]
            )
            with patch("app.api.scan.DeduplicationService") as dedup:
                dedup.return_value.check = AsyncMock(side_effect=lambda _d, _u, b: b)
                body = _post(client).json()
        assert body["enhanced_scans_remaining"] == 4
        assert "enhanced_scan_credits" not in body


class TestZeroResultAuthority:
    def test_forged_threshold_and_action_do_not_lift_the_throttle(
        self, client, store, provider, premium
    ):
        _settings(
            **{
                "scan.enhanced.zero_result_threshold": 1,
                "scan.enhanced.zero_result_action": "throttle",
            }
        )
        assert _post(client).status_code == 200  # first zero-result: free
        resp = _post(client)
        assert resp.status_code == 429
        assert resp.json() == {"detail": "enhanced_throttled"}
        assert provider.return_value.identify.await_count == 1

    def test_forged_threshold_does_not_make_zero_results_free(
        self, client, store, provider, premium
    ):
        _settings(**{"scan.enhanced.zero_result_threshold": 1})
        _post(client)
        _post(client)
        enhanced = store.count(feature=quota.ENHANCED_QUOTA_FEATURE, kind="month")
        assert enhanced == 1  # the second was charged
        assert len(store.zero_results[USER.id]) == 1

    def test_another_users_rows_never_count(self, client, store, provider, premium):
        _settings(
            **{
                "scan.enhanced.zero_result_threshold": 1,
                "scan.enhanced.zero_result_action": "throttle",
            }
        )
        store.zero_results[OTHER] = [None] * 10
        assert _post(client).status_code == 200
        assert store.zero_results[OTHER] == [None] * 10  # untouched
        assert len(store.zero_results[USER.id]) == 1

    def test_settings_are_not_exposed_by_usage(self, client, store, premium):
        _settings()
        body = client.get("/scan/usage", params=_FORGED).json()
        text = json.dumps(body)
        assert "threshold" not in text and "action" not in text
        assert "window" not in text

    def test_warning_through_scan_carries_no_user_or_request_data(
        self, client, store, provider, premium
    ):
        _settings(**{"scan.enhanced.zero_result_threshold": 1})
        with captured_app_events() as events:
            resp = client.post(
                "/scan",
                files={"file": ("s.jpg", io.BytesIO(_JPEG), "image/jpeg")},
                data={"enhanced": "true", "note": "shelf by the window"},
                headers={
                    "CF-Connecting-IP": "198.51.100.7",
                    "User-Agent": "Probe/1.0 (iPhone)",
                },
            )
        assert resp.status_code == 200
        warnings = [
            e
            for e in events
            if e.get("fingerprint", [None])[0] == "ai-enhanced-zero-results"
        ]
        (warning,) = warnings
        for key in ("request", "user", "breadcrumbs", "transaction"):
            assert key not in warning, key
        assert set(warning["tags"]) == {"feature", "action"}
        # sys.argv is added by the SDK's default argv integration (process
        # data, not request data).
        assert set(warning["extra"]) - {"sys.argv"} == {
            "count",
            "threshold",
            "window_hours",
        }
        text = json.dumps(warning)
        for leak in (
            str(USER.id),
            USER.email,
            "198.51.100.7",
            "Probe/1.0",
            "shelf by the window",
        ):
            assert leak not in text


class TestLegacyCreditsUnused:
    def test_no_app_code_reads_or_writes_enhanced_scan_credits(self):
        """Only the model column definition remains (dropped in #540)."""
        offenders = []
        for path in APP_ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                name = None
                if isinstance(node, ast.Attribute):
                    name = node.attr
                elif isinstance(node, ast.Name):
                    name = node.id
                elif isinstance(node, ast.keyword):
                    name = node.arg
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    name = node.value
                if name == "enhanced_scan_credits":
                    offenders.append((path.relative_to(APP_ROOT), node.lineno))
        # The one allowed occurrence: the column declaration in the model.
        assert [str(p) for p, _ in offenders] == ["models/user.py"]

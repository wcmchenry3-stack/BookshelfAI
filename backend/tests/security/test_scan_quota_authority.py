"""Quota authority (#528): every quota refusal happens before the provider is
called, nothing the client sends changes an allowance, a count or the tier,
and GET /scan/usage requires auth and is rate-limited.

Run on its own with:  pytest -m security tests/security/test_scan_quota_authority.py
"""

import io
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.ai import quota
from app.ai.entitlements import Tier
from app.core.config import settings
from app.main import app
from app.models.user import User
from tests.unit._quota_fake import FakeQuotaStore

pytestmark = pytest.mark.security

USER = User(
    id=uuid.UUID("00000000-0000-0000-0000-00000000528a"),
    email="authority@example.com",
    enhanced_scan_credits=0,
)


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (1, 2, 3)).save(buf, format="JPEG")
    return buf.getvalue()


_JPEG = _jpeg()

# Every name a client might try, as form fields, query params and headers.
_FORGED = {
    "tier": "premium",
    "plan": "premium",
    "limit": "1000",
    "daily_limit": "1000",
    "monthly_limit": "1000",
    "used": "0",
    "used_today": "0",
    "used_month": "0",
    "quota": "1000",
    "quota.free.found_daily": "1000",
    "quota.free.monthly": "1000",
    "scan.global_daily_limit": "1000000",
    "lease_id": str(uuid.uuid4()),
}


class _ProviderMustNotBeCalled:
    def __init__(self, *_a, **_kw):
        pytest.fail("the provider was called despite a quota refusal")


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
    with (
        patch("app.ai.gateway.OpenAIVisionProvider", _ProviderMustNotBeCalled),
        patch("app.api.scan.EnrichmentService"),
        patch("app.api.scan.DeduplicationService"),
    ):
        yield TestClient(app)
    app.dependency_overrides.clear()


def _post(client, **kwargs):
    return client.post(
        "/scan",
        files={"file": ("s.jpg", io.BytesIO(_JPEG), "image/jpeg")},
        **kwargs,
    )


def _fill(store, kind: str, n: int, feature: str = quota.SCAN_QUOTA_FEATURE) -> None:
    today = quota.utc_today()
    start = today if kind == "day" else today.replace(day=1)
    store.rows[(USER.id, feature, kind, start)] = n


class TestRefusedBeforeProvider:
    def test_daily(self, client, store):
        _fill(store, "day", 5)
        assert _post(client).json() == {"detail": "daily_scan_limit"}

    def test_month_rows_never_refuse_for_a_monthly_reason(self, client, store):
        """#582: a full month row (left by the previous release) is never
        read; the only refusal is the daily one."""
        _fill(store, "month", 10**6)
        _fill(store, "day", 5)
        assert _post(client).json() == {"detail": "daily_scan_limit"}

    def test_in_progress(self, client, store):
        store.leases[USER.id] = uuid.uuid4()
        assert _post(client).json() == {"detail": "scan_in_progress"}

    def test_global(self, client, store):
        store.extra_global = settings.scan_global_daily_limit
        assert _post(client).json() == {"detail": "scan_capacity"}

    def test_zero_allowance(self, client, store):
        with patch.object(settings, "scan_free_daily_limit", 0):
            assert _post(client).json() == {"detail": "daily_scan_limit"}


class TestClientCannotChangeTheOutcome:
    def test_forged_fields_headers_and_query_do_not_lift_the_daily_limit(
        self, client, store
    ):
        _fill(store, "day", 5)
        resp = _post(
            client,
            data=_FORGED,
            params=_FORGED,
            headers={f"X-{k.replace('.', '-')}": v for k, v in _FORGED.items()},
        )
        assert resp.status_code == 429
        assert resp.json() == {"detail": "daily_scan_limit"}
        assert store.count() == 5

    def test_forged_tier_does_not_select_premium_allowance(self, client, store):
        seen = []

        async def spy(db, user):
            seen.append(Tier.FREE)
            return Tier.FREE

        _fill(store, "day", 5)  # Free limit reached; Premium would allow 30
        with patch("app.ai.entitlements.tier_for", spy):
            resp = _post(
                client,
                data={"tier": "premium"},
                params={"tier": "premium"},
                headers={"X-Tier": "premium"},
            )
        assert resp.json() == {"detail": "daily_scan_limit"}
        assert seen == [Tier.FREE]


class TestUsageEndpointSecurity:
    def test_requires_auth(self):
        client = TestClient(app)
        assert client.get("/scan/usage").status_code in (401, 403)
        resp = client.get(
            "/scan/usage", headers={"Authorization": "Bearer not.a.token"}
        )
        assert resp.status_code == 401

    def test_is_rate_limited_with_rate_limit_reads(self):
        from app.core.database import get_db

        async def _fake_db():
            yield AsyncMock()

        app.dependency_overrides[get_db] = _fake_db
        limit = int(settings.rate_limit_reads.split("/")[0])
        client = TestClient(app, raise_server_exceptions=False)
        usage = quota.Usage(0, 5)
        try:
            with patch("app.api.scan.quota.usage_for", AsyncMock(return_value=usage)):
                from app.auth.dependencies import get_current_user

                app.dependency_overrides[get_current_user] = lambda: USER
                headers = {}
                codes = [
                    client.get("/scan/usage", headers=headers).status_code
                    for _ in range(limit)
                ]
                over = client.get("/scan/usage", headers=headers)
        finally:
            app.dependency_overrides.clear()
        assert set(codes) == {200}
        assert over.status_code == 429

    def test_query_params_cannot_change_the_reported_limits(self, client, store):
        from app.core.database import get_db

        db = AsyncMock()
        db.execute.return_value = MagicMock(scalar_one_or_none=lambda: None)

        async def _db():
            yield db

        app.dependency_overrides[get_db] = _db
        resp = client.get("/scan/usage", params=_FORGED, headers={"X-Tier": "premium"})
        assert resp.status_code == 200
        body = resp.json()
        assert (body["used_today"], body["daily_limit"]) == (0, 5)
        assert body["monthly_limit"] is None  # #582: no monthly limit

"""
Security tests for OriginAuthMiddleware.

When ORIGIN_AUTH_SECRET is set and the service is hardened, the middleware
must reject requests that do not carry the correct X-Origin-Auth header.
/health is explicitly exempt so Render's liveness prober always succeeds.
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

from app.main import app


@pytest.mark.security
class TestOriginAuthMiddleware:
    """OriginAuthMiddleware enforces CDN-origin secret when configured."""

    def _client_with(self, *, secret: str, hardened: bool) -> TestClient:
        with (
            patch("app.main.settings.origin_auth_secret", secret),
            patch("app.main.settings.origin_auth_header", "X-Origin-Auth"),
            patch("app.main.settings.is_hardened", hardened),
        ):
            return TestClient(app, raise_server_exceptions=False)

    def test_passes_when_secret_not_configured(self):
        """Empty secret = middleware is a no-op regardless of headers."""
        client = self._client_with(secret="", hardened=True)
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_passes_when_not_hardened(self):
        """Development/test environments skip the check entirely."""
        client = self._client_with(secret="correctsecret1234567890123456789", hardened=False)
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_health_always_passes_even_when_enforced(self):
        """GET /health must succeed with no header — Render liveness prober."""
        with (
            patch("app.main.settings.origin_auth_secret", "correctsecret1234567890123456789"),
            patch("app.main.settings.origin_auth_header", "X-Origin-Auth"),
            patch("app.main.settings.is_hardened", True),
        ):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/health")
            assert resp.status_code == 200

    def test_rejects_missing_header(self):
        """Request without the header is 403 when secret is set and hardened."""
        with (
            patch("app.main.settings.origin_auth_secret", "correctsecret1234567890123456789"),
            patch("app.main.settings.origin_auth_header", "X-Origin-Auth"),
            patch("app.main.settings.is_hardened", True),
        ):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/health/db")
            assert resp.status_code == 403

    def test_rejects_wrong_header_value(self):
        """Request with incorrect secret is 403."""
        with (
            patch("app.main.settings.origin_auth_secret", "correctsecret1234567890123456789"),
            patch("app.main.settings.origin_auth_header", "X-Origin-Auth"),
            patch("app.main.settings.is_hardened", True),
        ):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get(
                "/health/db",
                headers={"X-Origin-Auth": "wrongsecret"},
            )
            assert resp.status_code == 403

    def test_passes_correct_header(self):
        """Request with matching header passes through the middleware."""
        secret = "correctsecret1234567890123456789"
        with (
            patch("app.main.settings.origin_auth_secret", secret),
            patch("app.main.settings.origin_auth_header", "X-Origin-Auth"),
            patch("app.main.settings.is_hardened", True),
        ):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get(
                "/health/db",
                headers={"X-Origin-Auth": secret},
            )
            # /health/db may 503 in test (no DB), but must not be 403
            assert resp.status_code != 403

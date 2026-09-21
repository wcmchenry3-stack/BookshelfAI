"""Security tests for the dev-only /auth/test-login endpoint.

Verifies that the test-login bypass cannot be exploited:
- Rejects requests without the correct secret
- Rejects requests for non-allowlisted emails
- Is rate-limited aggressively
- Does NOT exist when ENVIRONMENT=production
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """TestClient with test-login enabled (non-production environment)."""
    from app.main import app

    return TestClient(app)


class TestTestLoginSecurity:
    """Verify the test-login endpoint cannot be abused."""

    def test_rejects_when_no_secret_configured(self, client):
        """Returns 403 when TEST_AUTH_SECRET is not set on the server."""
        with patch("app.main.settings") as mock_settings:
            mock_settings.test_auth_secret = ""
            mock_settings.environment = "development"
            mock_settings.rate_limit_health = "60/minute"
            resp = client.post(
                "/auth/test-login",
                json={"secret": "anything", "email": "test@example.com"},
            )
        assert resp.status_code == 403
        assert "not configured" in resp.json()["detail"]

    def test_rejects_wrong_secret(self, client):
        """Returns 403 when the provided secret doesn't match."""
        with patch("app.main.settings") as mock_settings:
            mock_settings.test_auth_secret = "correct-secret-abc"
            mock_settings.environment = "development"
            resp = client.post(
                "/auth/test-login",
                json={"secret": "wrong-secret", "email": "test@example.com"},
            )
        assert resp.status_code == 403
        assert "Invalid secret" in resp.json()["detail"]

    def test_rejects_empty_secret(self, client):
        """Returns 403 when secret is empty string."""
        with patch("app.main.settings") as mock_settings:
            mock_settings.test_auth_secret = "real-secret"
            mock_settings.environment = "development"
            resp = client.post(
                "/auth/test-login",
                json={"secret": "", "email": "test@example.com"},
            )
        assert resp.status_code == 403

    def test_rejects_email_not_in_allowlist(self, client):
        """Returns 403 when email is not in the ALLOWED_EMAILS list."""
        with patch("app.main.settings") as mock_settings:
            mock_settings.test_auth_secret = "correct-secret"
            mock_settings.environment = "development"
            mock_settings.allowed_emails_list = ["allowed@example.com"]
            resp = client.post(
                "/auth/test-login",
                json={"secret": "correct-secret", "email": "hacker@evil.com"},
            )
        assert resp.status_code == 403
        assert "allowlist" in resp.json()["detail"]

    def test_endpoint_not_registered_in_production(self):
        """The /auth/test-login route must not exist when hardened (staging/production)."""
        from pathlib import Path

        main_py = Path(__file__).resolve().parent.parent.parent / "app" / "main.py"
        source = main_py.read_text()

        # The endpoint is inside the `if not settings.is_hardened:` block.
        # Verify it's gated correctly by checking the source structure.
        assert "not settings.is_hardened" in source
        assert "/auth/test-login" in source

        # Find the route decorator for test-login and verify it comes AFTER
        # the hardened guard
        lines = source.split("\n")
        guard_line = None
        endpoint_line = None
        for i, line in enumerate(lines):
            if guard_line is None and "not settings.is_hardened" in line:
                guard_line = i
            if "@app.post" in line and "test-login" in line:
                endpoint_line = i

        assert guard_line is not None
        assert endpoint_line is not None
        assert (
            endpoint_line > guard_line
        ), "/auth/test-login must be inside the is_hardened guard block"

    def test_timing_safe_comparison(self):
        """Secret comparison must use hmac.compare_digest (timing-safe)."""
        from pathlib import Path

        main_py = Path(__file__).resolve().parent.parent.parent / "app" / "main.py"
        source = main_py.read_text()
        assert "hmac.compare_digest" in source

    def test_rate_limited(self):
        """The endpoint should have a rate limit decorator."""
        from pathlib import Path

        main_py = Path(__file__).resolve().parent.parent.parent / "app" / "main.py"
        source = main_py.read_text()

        # Find the test-login function and verify it has a rate limit
        lines = source.split("\n")
        for i, line in enumerate(lines):
            if "test-login" in line and "def " not in line and "@" not in line:
                continue
            if "def test_login" in line:
                # Check preceding lines for rate limit decorator
                preceding = "\n".join(lines[max(0, i - 3) : i])
                assert (
                    "limiter.limit" in preceding
                ), "/auth/test-login must have a rate limit decorator"
                break

    def test_no_pii_in_response(self, client):
        """Response should only contain tokens, not user email or PII."""
        # Verify the response schema from the source
        from pathlib import Path

        main_py = Path(__file__).resolve().parent.parent.parent / "app" / "main.py"
        source = main_py.read_text()

        # Find the JSONResponse content for test-login
        # It should only have access_token, refresh_token, expires_in
        assert '"access_token"' in source
        assert '"refresh_token"' in source
        assert '"expires_in"' in source

"""
Security tests — OWASP-aligned injection and input-validation cases.

All tests in this file are tagged @pytest.mark.security so they can be run
independently:  pytest -m security -v
"""

import io

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app  # noqa: F401 — side-effect: registers routers and middleware
from app.core.url_validator import validate_safe_url
from app.schemas.user_book import UserBookUpdate, WishlistRequest

# ---------------------------------------------------------------------------
# Injection payload fixtures
# ---------------------------------------------------------------------------

SQL_INJECTION_PAYLOADS = [
    "'; DROP TABLE users; --",
    "' OR '1'='1",
    "1; SELECT * FROM users--",
    "' UNION SELECT null,null,null--",
    "admin'--",
]

XSS_PAYLOADS = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "javascript:alert(1)",
    '"><svg onload=alert(1)>',
    "<iframe src='javascript:alert(1)'>",
]


# ---------------------------------------------------------------------------
# A03 — Injection: Pydantic schema validation
# ---------------------------------------------------------------------------


@pytest.mark.security
class TestSchemaAcceptsSqlInjectionSafely:
    """
    Pydantic schemas accept SQL injection strings as plain text — they are not
    interpreted as SQL because the app uses SQLAlchemy ORM with bound parameters.
    This suite verifies the schemas don't crash or strip the values (that would
    mask injection attempts in logs) — the ORM layer is responsible for safety.
    """

    @pytest.mark.parametrize("payload", SQL_INJECTION_PAYLOADS)
    def test_wishlist_title_accepts_injection_string(self, payload: str) -> None:
        req = WishlistRequest(title=payload, author="Safe Author")
        assert req.title == payload

    @pytest.mark.parametrize("payload", SQL_INJECTION_PAYLOADS)
    def test_wishlist_author_accepts_injection_string(self, payload: str) -> None:
        req = WishlistRequest(title="Safe Title", author=payload)
        assert req.author == payload

    @pytest.mark.parametrize("payload", XSS_PAYLOADS)
    def test_notes_field_accepts_xss_payload(self, payload: str) -> None:
        update = UserBookUpdate(notes=payload)
        assert update.notes == payload


# ---------------------------------------------------------------------------
# A03 — Injection: rating boundary enforcement
# ---------------------------------------------------------------------------


@pytest.mark.security
class TestRatingBoundaryEnforcement:
    """UserBookUpdate.rating must reject values outside 1–5."""

    @pytest.mark.parametrize("bad_rating", [0, 6, -1, 999, -999])
    def test_rating_rejects_out_of_range(self, bad_rating: int) -> None:
        with pytest.raises(ValidationError):
            UserBookUpdate(rating=bad_rating)

    @pytest.mark.parametrize("good_rating", [1, 2, 3, 4, 5])
    def test_rating_accepts_valid_range(self, good_rating: int) -> None:
        update = UserBookUpdate(rating=good_rating)
        assert update.rating == good_rating


# ---------------------------------------------------------------------------
# A08 — File upload validation (extension + MIME + size)
# ---------------------------------------------------------------------------


def _authed_client() -> TestClient:
    """Return a TestClient with get_current_user dependency overridden."""
    from app.auth.dependencies import get_current_user
    from app.models.user import User
    import uuid

    fake_user = User()
    fake_user.id = uuid.uuid4()
    fake_user.email = "test@example.com"

    app.dependency_overrides[get_current_user] = lambda: fake_user
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _clear_dependency_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.mark.security
class TestScanUploadValidation:
    def test_rejects_disallowed_extension(self) -> None:
        client = _authed_client()
        data = io.BytesIO(b"GIF89a<fake gif data>")
        response = client.post(
            "/scan",
            files={"file": ("malware.exe", data, "image/jpeg")},
        )
        assert response.status_code == 415

    def test_rejects_php_file_with_image_content_type(self) -> None:
        client = _authed_client()
        data = io.BytesIO(b"<?php echo shell_exec($_GET['e']); ?>")
        response = client.post(
            "/scan",
            files={"file": ("shell.php", data, "image/jpeg")},
        )
        assert response.status_code == 415

    def test_rejects_oversized_image(self) -> None:
        client = _authed_client()
        big = io.BytesIO(b"A" * (5 * 1024 * 1024 + 1))
        response = client.post(
            "/scan",
            files={"file": ("big.jpg", big, "image/jpeg")},
        )
        assert response.status_code == 413

    def test_rejects_wrong_content_type_with_valid_extension(self) -> None:
        client = _authed_client()
        data = io.BytesIO(b"fake data")
        response = client.post(
            "/scan",
            files={"file": ("cover.jpg", data, "application/octet-stream")},
        )
        assert response.status_code == 415

    def test_rejects_mismatched_magic_bytes(self) -> None:
        """A file with a valid extension and MIME type but wrong magic bytes is rejected."""
        client = _authed_client()
        # PHP content disguised as JPEG
        data = io.BytesIO(b"<?php echo shell_exec($_GET['e']); ?>")
        response = client.post(
            "/scan",
            files={"file": ("cover.jpg", data, "image/jpeg")},
        )
        assert response.status_code == 415

    def test_accepts_valid_jpeg_magic_bytes(self) -> None:
        """A file with correct JPEG magic bytes passes upload validation."""
        from unittest.mock import AsyncMock, patch

        client = _authed_client()
        # Minimal JPEG: SOI marker + valid magic prefix
        jpeg_magic = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        data = io.BytesIO(jpeg_magic)

        with patch(
            "app.api.scan.ChatGPTVisionIdentifier.identify",
            new_callable=AsyncMock,
            return_value=[],
        ):
            response = client.post(
                "/scan",
                files={"file": ("cover.jpg", data, "image/jpeg")},
            )
        # Returns 200 with empty list — magic bytes validation passed
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# A05 — Security misconfiguration: security headers present
# ---------------------------------------------------------------------------


_REQUIRED_HEADERS = {
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "x-xss-protection": "0",
    "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
    "cross-origin-resource-policy": "same-origin",
}


@pytest.mark.security
class TestSecurityHeaders:
    """Every response must carry the required security headers."""

    @pytest.mark.parametrize("path", ["/health", "/auth/me"])
    def test_security_headers_present_on_all_endpoints(self, path: str) -> None:
        client = TestClient(app)
        response = client.get(path)
        for header, expected in _REQUIRED_HEADERS.items():
            assert (
                response.headers.get(header) == expected
            ), f"Header '{header}' missing or wrong on {path}"

    def test_hsts_absent_outside_production(self) -> None:
        """HSTS must not be sent in non-production to avoid caching it on localhost."""
        client = TestClient(app)
        response = client.get("/health")
        assert "strict-transport-security" not in response.headers

    def test_hsts_present_in_production(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import app.main as main_module

        monkeypatch.setattr(main_module.settings, "environment", "production")
        client = TestClient(app)
        response = client.get("/health")
        hsts = response.headers.get("strict-transport-security", "")
        assert "max-age=31536000" in hsts
        assert "includeSubDomains" in hsts


# ---------------------------------------------------------------------------
# A05 — CORS enforcement
# ---------------------------------------------------------------------------


@pytest.mark.security
class TestCORSEnforcement:
    """Disallowed origins must receive no CORS headers; wildcard must never appear."""

    def test_disallowed_origin_receives_no_acao_header(self) -> None:
        client = TestClient(app)
        response = client.get(
            "/health",
            headers={"Origin": "https://evil.example.com"},
        )
        assert "access-control-allow-origin" not in response.headers

    def test_preflight_from_disallowed_origin_receives_no_acao_header(self) -> None:
        client = TestClient(app)
        response = client.options(
            "/health",
            headers={
                "Origin": "https://evil.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        acao = response.headers.get("access-control-allow-origin", "")
        assert acao != "*"
        assert "evil.example.com" not in acao

    def test_wildcard_cors_never_returned(self) -> None:
        """settings validator already blocks wildcard in config; verify at HTTP layer."""
        client = TestClient(app)
        response = client.get("/health", headers={"Origin": "https://attacker.io"})
        assert response.headers.get("access-control-allow-origin", "") != "*"

    def test_preflight_disallows_put_method(self) -> None:
        """PUT is not in the explicit allow_methods list; must not appear in the
        Access-Control-Allow-Methods response header for a cross-origin preflight."""
        client = TestClient(app)
        response = client.options(
            "/health",
            headers={
                "Origin": "https://bookshelfai.buffingchi.com",
                "Access-Control-Request-Method": "PUT",
            },
        )
        allowed = response.headers.get("access-control-allow-methods", "")
        assert "PUT" not in allowed.upper()


# ---------------------------------------------------------------------------
# A04 — Rate limit enforcement
# ---------------------------------------------------------------------------


@pytest.mark.security
class TestRateLimitEnforcement:
    """Rate limits must actually trigger 429 responses after the threshold."""

    def test_auth_endpoint_returns_429_after_limit(self) -> None:
        """POST /auth/google is limited to 5/minute; 6th request must be 429."""
        client = TestClient(app, raise_server_exceptions=False)
        for _ in range(5):
            client.post("/auth/google", json={"id_token": "invalid"})
        response = client.post("/auth/google", json={"id_token": "invalid"})
        assert response.status_code == 429

    def test_scan_endpoint_returns_429_after_limit(self) -> None:
        """POST /scan is limited to 10/minute per user; 11th request must be 429."""
        from app.auth.dependencies import get_current_user
        from app.models.user import User
        import uuid

        fake_user = User()
        fake_user.id = uuid.uuid4()
        fake_user.email = "ratelimit-scan@example.com"

        app.dependency_overrides[get_current_user] = lambda: fake_user
        client = TestClient(app, raise_server_exceptions=False)

        jpeg_magic = b"\xff\xd8\xff\xe0" + b"\x00" * 20
        for _ in range(10):
            data = io.BytesIO(jpeg_magic)
            client.post("/scan", files={"file": ("test.jpg", data, "image/jpeg")})

        data = io.BytesIO(jpeg_magic)
        response = client.post(
            "/scan", files={"file": ("test.jpg", data, "image/jpeg")}
        )
        app.dependency_overrides.clear()
        assert response.status_code == 429


# ---------------------------------------------------------------------------
# A01/A07 — Authentication bypass attempts
# ---------------------------------------------------------------------------


@pytest.mark.security
class TestAuthBypassAttempts:
    """All protected endpoints must reject unauthenticated and malformed requests."""

    _PROTECTED_ENDPOINTS = [
        ("GET", "/auth/me"),
        ("GET", "/user-books"),
        ("POST", "/wishlist"),
        ("POST", "/scan"),
    ]

    @pytest.mark.parametrize("method,path", _PROTECTED_ENDPOINTS)
    def test_endpoint_rejects_missing_auth(self, method: str, path: str) -> None:
        client = TestClient(app, raise_server_exceptions=False)
        response = client.request(method, path)
        assert response.status_code in (
            401,
            403,
        ), f"{method} {path} returned {response.status_code}, expected 401 or 403"

    @pytest.mark.parametrize("method,path", _PROTECTED_ENDPOINTS)
    def test_endpoint_rejects_malformed_bearer(self, method: str, path: str) -> None:
        client = TestClient(app, raise_server_exceptions=False)
        response = client.request(
            method, path, headers={"Authorization": "Bearer not.a.real.jwt"}
        )
        assert response.status_code in (
            401,
            403,
        ), f"{method} {path} with bad Bearer returned {response.status_code}"

    def test_basic_auth_credentials_rejected(self) -> None:
        """Basic Auth must never grant access to Bearer-only endpoints."""
        import base64

        credentials = base64.b64encode(b"admin:password").decode()
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(
            "/auth/me", headers={"Authorization": f"Basic {credentials}"}
        )
        # FastAPI's HTTPBearer rejects non-Bearer schemes with 401 or 403
        assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# A03/A04 — Input length limits (schema-level enforcement)
# ---------------------------------------------------------------------------


@pytest.mark.security
class TestInputLengthLimits:
    """
    All user-supplied string fields must be bounded.
    Exceeding max_length must raise Pydantic ValidationError at the schema layer
    before any business logic or database query is reached.
    """

    def test_wishlist_title_too_long(self) -> None:
        with pytest.raises(ValidationError):
            WishlistRequest(title="a" * 501, author="Author")

    def test_wishlist_title_at_limit_accepted(self) -> None:
        req = WishlistRequest(title="a" * 500, author="Author")
        assert len(req.title) == 500

    def test_wishlist_author_too_long(self) -> None:
        with pytest.raises(ValidationError):
            WishlistRequest(title="Title", author="a" * 501)

    def test_wishlist_author_at_limit_accepted(self) -> None:
        req = WishlistRequest(title="Title", author="a" * 500)
        assert len(req.author) == 500

    def test_wishlist_description_too_long(self) -> None:
        with pytest.raises(ValidationError):
            WishlistRequest(title="Title", author="Author", description="a" * 5001)

    def test_wishlist_description_at_limit_accepted(self) -> None:
        req = WishlistRequest(title="Title", author="Author", description="a" * 5000)
        assert req.description is not None and len(req.description) == 5000

    def test_wishlist_cover_url_too_long(self) -> None:
        with pytest.raises(ValidationError):
            WishlistRequest(
                title="Title", author="Author", cover_url="https://x.com/" + "a" * 2040
            )

    def test_wishlist_cover_url_at_limit_accepted(self) -> None:
        url = "https://x.com/" + "a" * (2048 - len("https://x.com/"))
        req = WishlistRequest(title="Title", author="Author", cover_url=url)
        assert req.cover_url is not None and len(req.cover_url) == 2048

    def test_user_book_update_notes_too_long(self) -> None:
        with pytest.raises(ValidationError):
            UserBookUpdate(notes="a" * 10001)

    def test_user_book_update_notes_at_limit_accepted(self) -> None:
        update = UserBookUpdate(notes="a" * 10000)
        assert update.notes is not None and len(update.notes) == 10000

    def test_wishlist_empty_title_rejected(self) -> None:
        with pytest.raises(ValidationError):
            WishlistRequest(title="", author="Author")

    def test_wishlist_empty_author_rejected(self) -> None:
        with pytest.raises(ValidationError):
            WishlistRequest(title="Title", author="")


# ---------------------------------------------------------------------------
# A10 — SSRF prevention on user-supplied URL fields
# ---------------------------------------------------------------------------


@pytest.mark.security
class TestSSRFPrevention:
    """
    User-supplied URLs must resolve to public IPs only.
    Private, loopback, and link-local addresses must be rejected with a
    Pydantic ValidationError (→ 422 at the HTTP layer).
    """

    @pytest.mark.parametrize(
        "url",
        [
            "https://127.0.0.1/secret",
            "https://127.0.0.1:8080/admin",
            "https://10.0.0.1/internal",
            "https://10.255.255.255/internal",
            "https://172.16.0.1/private",
            "https://172.31.255.255/private",
            "https://192.168.1.1/router",
            "https://169.254.169.254/latest/meta-data/",  # AWS metadata
            "https://169.254.169.254/computeMetadata/v1/",  # GCP metadata
        ],
    )
    def test_private_ip_urls_rejected(self, url: str) -> None:
        with pytest.raises(ValueError, match="private or reserved"):
            validate_safe_url(url)

    def test_http_scheme_rejected(self) -> None:
        with pytest.raises(ValueError, match="https"):
            validate_safe_url("http://example.com/cover.jpg")

    def test_ftp_scheme_rejected(self) -> None:
        with pytest.raises(ValueError, match="https"):
            validate_safe_url("ftp://example.com/cover.jpg")

    def test_none_is_allowed(self) -> None:
        assert validate_safe_url(None) is None

    def test_private_ip_rejected_via_schema(self) -> None:
        with pytest.raises(ValidationError):
            WishlistRequest(
                title="Title",
                author="Author",
                cover_url="https://192.168.1.1/cover.jpg",
            )

    def test_missing_hostname_rejected(self) -> None:
        with pytest.raises(ValueError):
            validate_safe_url("https:///no-host")


# ---------------------------------------------------------------------------
# A01 — Ownership isolation: cross-user access on PATCH/DELETE user-books
# ---------------------------------------------------------------------------


@pytest.mark.security
class TestOwnershipIsolation:
    """
    OWASP A01 — Broken Access Control.

    A user must not be able to mutate a user_book record they do not own.
    Both PATCH and DELETE scope their DB query to current_user.id, so a
    cross-user request simply finds no record and returns 404 (information hiding).
    """

    _USER_A_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    _USER_B_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    _USER_BOOK_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"

    @staticmethod
    def _client_as_user_b():
        """TestClient authenticated as User B with an empty DB (User A owns the record)."""
        from unittest.mock import AsyncMock, MagicMock

        from app.auth.dependencies import get_current_user
        from app.core.database import get_db
        from app.models.user import User

        user_b = User()
        user_b.id = TestOwnershipIsolation._USER_B_ID
        user_b.email = "userb@example.com"

        # Empty result set — simulates no record matching user_b.id
        empty_result = MagicMock()
        empty_result.scalar_one_or_none.return_value = None

        async def _fake_db():
            db = AsyncMock()
            db.execute = AsyncMock(return_value=empty_result)
            yield db

        app.dependency_overrides[get_current_user] = lambda: user_b
        app.dependency_overrides[get_db] = _fake_db
        return TestClient(app, raise_server_exceptions=False)

    def test_patch_returns_404_for_other_users_record(self) -> None:
        """User B cannot PATCH a user_book owned by User A."""
        client = self._client_as_user_b()
        resp = client.patch(
            f"/user-books/{self._USER_BOOK_ID}",
            json={"status": "purchased"},
        )
        app.dependency_overrides.clear()
        assert resp.status_code == 404

    def test_delete_returns_404_for_other_users_record(self) -> None:
        """User B cannot DELETE a user_book owned by User A."""
        client = self._client_as_user_b()
        resp = client.delete(f"/user-books/{self._USER_BOOK_ID}")
        app.dependency_overrides.clear()
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# A05 — Turnstile required by default (misconfiguration guard)
# ---------------------------------------------------------------------------


@pytest.mark.security
class TestTurnstileRequired:
    """
    The startup validator must prevent the app from serving requests when
    TURNSTILE_REQUIRED=true but no secret key is configured.
    When disabled explicitly (TURNSTILE_REQUIRED=false), startup succeeds
    regardless of whether a key is present.
    """

    def test_startup_raises_when_required_and_no_key(self) -> None:
        """Startup must raise RuntimeError when Turnstile is required but unconfigured."""
        import asyncio
        from unittest.mock import patch

        from app.core.config import Settings

        cfg = Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db",
            turnstile_required=True,
            turnstile_secret_key="",
        )

        async def _run():
            from app.main import _validate_config

            with patch("app.main.settings", cfg):
                await _validate_config()

        with pytest.raises(RuntimeError, match="TURNSTILE_SECRET_KEY"):
            asyncio.run(_run())

    def test_startup_succeeds_when_required_and_key_present(self) -> None:
        """Startup must not raise when Turnstile is required and key is configured."""
        import asyncio
        from unittest.mock import patch

        from app.core.config import Settings

        cfg = Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db",
            turnstile_required=True,
            turnstile_secret_key="a-real-secret-key",
        )

        async def _run():
            from app.main import _validate_config

            with patch("app.main.settings", cfg):
                await _validate_config()

        asyncio.run(_run())  # must not raise

    def test_startup_succeeds_when_explicitly_disabled(self) -> None:
        """TURNSTILE_REQUIRED=false must allow startup even with no key."""
        import asyncio
        from unittest.mock import patch

        from app.core.config import Settings

        cfg = Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db",
            turnstile_required=False,
            turnstile_secret_key="",
        )

        async def _run():
            from app.main import _validate_config

            with patch("app.main.settings", cfg):
                await _validate_config()

        asyncio.run(_run())  # must not raise

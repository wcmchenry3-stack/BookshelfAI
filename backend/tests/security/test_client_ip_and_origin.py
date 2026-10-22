"""Security tests — client-IP resolution, rate-limit keys and origin lockdown.

Run independently with:  pytest -m security -v
"""

import logging
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import Request

from app.core import client_ip
from app.core.client_ip import client_ip_bucket, parse_ip, resolve_client_ip
from app.core.config import Settings, settings
from app.core.limiter import _get_user_id_or_ip, limiter
from app.core.origin_auth import OriginAuthMiddleware

pytestmark = pytest.mark.security

PEER = "203.0.113.9"
FORGED_CF = "198.51.100.20"
FORGED_XFF = "198.51.100.10"
SECRET = "unit-test-origin-secret-value-0123456789"
DB_URL = "postgresql+asyncpg://test:test@localhost/test"


def _request(headers: list[tuple[str, str]] | None = None, peer: str | None = PEER):
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [
            (k.lower().encode("latin-1"), v.encode("latin-1"))
            for k, v in (headers or [])
        ],
        "client": (peer, 1234) if peer is not None else None,
    }
    return Request(scope)


@pytest.fixture
def proxy_mode(monkeypatch):
    def _set(mode: str) -> None:
        monkeypatch.setattr(settings, "trusted_proxy_mode", mode)

    return _set


# ---------------------------------------------------------------------------
# Resolver: "none" mode
# ---------------------------------------------------------------------------


class TestDefaultMode:
    """Unset TRUSTED_PROXY_MODE: cloudflare when hardened, none otherwise."""

    @pytest.fixture(autouse=True)
    def _unset_env(self, monkeypatch, tmp_path):
        monkeypatch.delenv("TRUSTED_PROXY_MODE", raising=False)
        # Keep a developer's local backend/.env out of Settings().
        monkeypatch.chdir(tmp_path)

    @pytest.mark.parametrize(
        ("environment", "expected"),
        [
            ("development", "none"),
            ("test", "none"),
            ("staging", "cloudflare"),
            ("production", "cloudflare"),
        ],
    )
    def test_unset_defaults_by_environment(self, environment, expected):
        s = Settings(database_url=DB_URL, environment=environment)
        assert s.trusted_proxy_mode == expected

    def test_explicit_none_in_production_is_kept(self):
        s = Settings(
            database_url=DB_URL, environment="production", trusted_proxy_mode="none"
        )
        assert s.trusted_proxy_mode == "none"

    def test_explicit_cloudflare_in_development_is_kept(self):
        s = Settings(
            database_url=DB_URL,
            environment="development",
            trusted_proxy_mode="cloudflare",
        )
        assert s.trusted_proxy_mode == "cloudflare"

    @pytest.mark.parametrize(
        ("environment", "expected"),
        [("development", "none"), ("production", "cloudflare")],
    )
    def test_empty_string_env_is_treated_as_unset(
        self, monkeypatch, environment, expected
    ):
        monkeypatch.setenv("TRUSTED_PROXY_MODE", "")
        s = Settings(database_url=DB_URL, environment=environment)
        assert s.trusted_proxy_mode == expected

    def test_invalid_value_fails(self):
        with pytest.raises(ValidationError):
            Settings(
                database_url=DB_URL, environment="production", trusted_proxy_mode="x"
            )


class TestNoneMode:
    def test_uses_socket_peer(self, proxy_mode):
        proxy_mode("none")
        assert resolve_client_ip(_request()) == PEER

    def test_ignores_forged_cf_connecting_ip(self, proxy_mode):
        proxy_mode("none")
        req = _request([("CF-Connecting-IP", FORGED_CF)])
        assert resolve_client_ip(req) == PEER

    def test_ignores_forged_x_forwarded_for(self, proxy_mode):
        proxy_mode("none")
        req = _request([("X-Forwarded-For", f"{FORGED_XFF}, {FORGED_CF}")])
        assert resolve_client_ip(req) == PEER

    def test_missing_peer_is_unknown(self, proxy_mode):
        proxy_mode("none")
        assert resolve_client_ip(_request(peer=None)) == "unknown"


# ---------------------------------------------------------------------------
# Resolver: "cloudflare" mode
# ---------------------------------------------------------------------------


class TestCloudflareMode:
    def test_trusts_single_valid_cf_connecting_ip(self, proxy_mode):
        proxy_mode("cloudflare")
        req = _request([("CF-Connecting-IP", FORGED_CF)])
        assert resolve_client_ip(req) == FORGED_CF

    def test_ignores_x_forwarded_for_entirely(self, proxy_mode):
        proxy_mode("cloudflare")
        req = _request([("X-Forwarded-For", FORGED_XFF)])
        assert resolve_client_ip(req) == PEER

    def test_cf_header_wins_over_x_forwarded_for(self, proxy_mode):
        proxy_mode("cloudflare")
        req = _request(
            [("X-Forwarded-For", FORGED_XFF), ("CF-Connecting-IP", FORGED_CF)]
        )
        assert resolve_client_ip(req) == FORGED_CF

    def test_repeated_cf_header_falls_back_to_peer(self, proxy_mode):
        proxy_mode("cloudflare")
        req = _request(
            [("CF-Connecting-IP", FORGED_CF), ("CF-Connecting-IP", FORGED_XFF)]
        )
        assert resolve_client_ip(req) == PEER

    @pytest.mark.parametrize(
        "value",
        [
            f"{FORGED_CF}, {FORGED_XFF}",
            "not-an-ip",
            "",
            "   ",
            "198.51.100.20:443",
            "[2001:db8::1]",
            "fe80::1%eth0",
            "999.1.1.1",
            "198.51.100.20 extra",
            "1" * 200,
        ],
    )
    def test_malformed_cf_header_falls_back_to_peer(self, proxy_mode, value):
        proxy_mode("cloudflare")
        req = _request([("CF-Connecting-IP", value)])
        assert resolve_client_ip(req) == PEER

    def test_canonicalises_ipv6(self, proxy_mode):
        proxy_mode("cloudflare")
        req = _request([("CF-Connecting-IP", " 2001:DB8:0:0::0001 ")])
        assert resolve_client_ip(req) == "2001:db8::1"

    def test_ipv4_mapped_ipv6_becomes_ipv4(self, proxy_mode):
        proxy_mode("cloudflare")
        req = _request([("CF-Connecting-IP", "::ffff:198.51.100.20")])
        assert resolve_client_ip(req) == FORGED_CF


class TestParseIp:
    def test_none_is_none(self):
        assert parse_ip(None) is None

    def test_valid_ipv4(self):
        assert str(parse_ip("192.0.2.1")) == "192.0.2.1"


# ---------------------------------------------------------------------------
# Rate-limit bucket
# ---------------------------------------------------------------------------


class TestBucket:
    def test_ipv4_bucket_is_the_address(self, proxy_mode):
        proxy_mode("none")
        assert client_ip_bucket(_request(peer="192.0.2.7")) == "192.0.2.7"

    def test_ipv6_same_64_shares_bucket(self, proxy_mode):
        proxy_mode("cloudflare")
        a = client_ip_bucket(_request([("CF-Connecting-IP", "2001:db8:1:2::1")]))
        b = client_ip_bucket(
            _request([("CF-Connecting-IP", "2001:db8:1:2:ffff:ffff:ffff:ffff")])
        )
        assert a == b == "2001:db8:1:2::/64"

    def test_ipv6_different_64_gets_different_bucket(self, proxy_mode):
        proxy_mode("cloudflare")
        a = client_ip_bucket(_request([("CF-Connecting-IP", "2001:db8:1:2::1")]))
        b = client_ip_bucket(_request([("CF-Connecting-IP", "2001:db8:1:3::1")]))
        assert a != b

    def test_ipv6_peer_bucketed_in_none_mode(self, proxy_mode):
        proxy_mode("none")
        assert client_ip_bucket(_request(peer="2001:db8::5")) == "2001:db8::/64"

    def test_non_ip_peer_passed_through(self, proxy_mode):
        proxy_mode("none")
        assert client_ip_bucket(_request(peer="testclient")) == "testclient"


# ---------------------------------------------------------------------------
# Rate-limit key: only access tokens give a per-user bucket
# ---------------------------------------------------------------------------


@pytest.fixture
def jwt_keys(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    monkeypatch.setattr(settings, "jwt_private_key", private_pem)
    monkeypatch.setattr(settings, "jwt_public_key", public_pem)
    return private_pem


class TestRateLimitKey:
    def test_access_token_keys_by_user(self, jwt_keys, proxy_mode):
        from app.auth.jwt import create_access_token

        proxy_mode("none")
        token = create_access_token("user-1", "a@example.com")
        req = _request([("Authorization", f"Bearer {token}")])
        assert _get_user_id_or_ip(req) == "user:user-1"

    def test_refresh_token_falls_back_to_ip(self, jwt_keys, proxy_mode):
        from app.auth.jwt import create_refresh_token

        proxy_mode("none")
        token, _ = create_refresh_token("user-1")
        req = _request([("Authorization", f"Bearer {token}")])
        assert _get_user_id_or_ip(req) == PEER

    def test_token_without_type_falls_back_to_ip(self, jwt_keys, proxy_mode):
        proxy_mode("none")
        now = datetime.now(UTC)
        token = jwt.encode(
            {"sub": "user-1", "iat": now, "exp": now + timedelta(hours=1)},
            jwt_keys,
            algorithm="RS256",
        )
        req = _request([("Authorization", f"Bearer {token}")])
        assert _get_user_id_or_ip(req) == PEER

    def test_invalid_token_uses_resolved_ip(self, jwt_keys, proxy_mode):
        proxy_mode("cloudflare")
        req = _request(
            [("Authorization", "Bearer garbage"), ("CF-Connecting-IP", FORGED_CF)]
        )
        assert _get_user_id_or_ip(req) == FORGED_CF


# ---------------------------------------------------------------------------
# End to end through slowapi: forged headers can't open new buckets
# ---------------------------------------------------------------------------


def _build_limited_app() -> FastAPI:
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded

    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    @app.get("/limited")
    @limiter.limit("2/minute")
    async def limited(request: Request) -> dict:
        return {"ok": True}

    return app


# Built once: slowapi registers limits per decorated function name, so
# re-decorating in every test would stack duplicate limits.
_LIMITED_APP = _build_limited_app()


def _limited_app(with_origin_auth: bool = False):
    return OriginAuthMiddleware(_LIMITED_APP) if with_origin_auth else _LIMITED_APP


class TestRateLimitEndToEnd:
    def test_none_mode_rotating_forged_headers_share_one_bucket(self, proxy_mode):
        proxy_mode("none")
        client = TestClient(_limited_app())
        codes = [
            client.get(
                "/limited",
                headers={
                    "CF-Connecting-IP": f"198.51.100.{i}",
                    "X-Forwarded-For": f"192.0.2.{i}",
                },
            ).status_code
            for i in range(4)
        ]
        assert codes == [200, 200, 429, 429]

    def test_cloudflare_mode_rotating_xff_shares_one_bucket(self, proxy_mode):
        proxy_mode("cloudflare")
        client = TestClient(_limited_app())
        codes = [
            client.get(
                "/limited", headers={"X-Forwarded-For": f"192.0.2.{i}"}
            ).status_code
            for i in range(4)
        ]
        assert codes == [200, 200, 429, 429]

    def test_cloudflare_mode_keys_by_cf_connecting_ip(self, proxy_mode):
        proxy_mode("cloudflare")
        client = TestClient(_limited_app())
        a = [
            client.get("/limited", headers={"CF-Connecting-IP": FORGED_CF}).status_code
            for _ in range(3)
        ]
        b = client.get("/limited", headers={"CF-Connecting-IP": FORGED_XFF})
        assert a == [200, 200, 429]
        assert b.status_code == 200


# ---------------------------------------------------------------------------
# Settings validation
# ---------------------------------------------------------------------------


class TestSettingsValidation:
    @pytest.fixture(autouse=True)
    def _no_local_dotenv(self, monkeypatch, tmp_path):
        # Keep a developer's local backend/.env out of Settings().
        monkeypatch.chdir(tmp_path)

    def test_invalid_trusted_proxy_mode_fails(self):
        with pytest.raises(ValidationError):
            Settings(database_url=DB_URL, trusted_proxy_mode="render")

    def test_invalid_trusted_proxy_mode_from_env_fails(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXY_MODE", "cloudflare,none")
        with pytest.raises(ValidationError):
            Settings(database_url=DB_URL)

    def test_valid_trusted_proxy_mode_from_env(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXY_MODE", "cloudflare")
        assert Settings(database_url=DB_URL).trusted_proxy_mode == "cloudflare"

    @pytest.mark.parametrize("name", ["", "Bad Header", "X-Origin:Auth"])
    def test_invalid_origin_header_name_fails(self, name):
        with pytest.raises(ValidationError):
            Settings(database_url=DB_URL, origin_auth_header=name)

    @pytest.mark.parametrize(
        ("value", "rule"),
        [
            (" " + "a" * 40, "whitespace"),
            ("a" * 40 + "\n", "whitespace"),
            ("a" * 20 + "\u00e9" + "a" * 20, "printable ASCII"),
            ("a" * 20 + "\x01" + "a" * 20, "printable ASCII"),
            ("a" * 31, "at least 32"),
        ],
    )
    def test_invalid_origin_auth_secret_fails_without_echoing_it(self, value, rule):
        with pytest.raises(ValidationError) as exc_info:
            Settings(database_url=DB_URL, origin_auth_secret=value)
        message = str(exc_info.value)
        assert rule in message
        assert value.strip() not in message
        assert "a" * 20 not in message

    def test_invalid_origin_auth_secret_from_env_fails(self, monkeypatch):
        monkeypatch.setenv("ORIGIN_AUTH_SECRET", "too-short")
        with pytest.raises(ValidationError) as exc_info:
            Settings(database_url=DB_URL)
        assert "too-short" not in str(exc_info.value)

    def test_valid_origin_auth_secret_accepted(self):
        value = "0123456789abcdef" * 4  # e.g. `openssl rand -hex 32`
        assert (
            Settings(database_url=DB_URL, origin_auth_secret=value).origin_auth_secret
            == value
        )

    def test_origin_auth_secret_of_exactly_min_length_accepted(self):
        value = "A" * 32
        assert (
            Settings(database_url=DB_URL, origin_auth_secret=value).origin_auth_secret
            == value
        )

    def test_origin_auth_disabled_by_default(self):
        s = Settings(database_url=DB_URL)
        assert s.origin_auth_secret == ""
        assert s.origin_auth_header == "X-Origin-Auth"


# ---------------------------------------------------------------------------
# Origin lockdown, through the real app
# ---------------------------------------------------------------------------


@pytest.fixture
def origin_secret(monkeypatch):
    monkeypatch.setattr(settings, "origin_auth_secret", SECRET)
    return SECRET


@pytest.fixture
def real_client():
    from app.main import app

    return TestClient(app)


CORS_ORIGIN = "https://app.example.test"


@pytest.fixture
def cors_client(monkeypatch):
    """The real app, with CORSMiddleware allowing CORS_ORIGIN.

    CORSMiddleware copies settings.cors_origins when app.main is imported (empty
    in tests), so this patches the registered middleware's options and forces
    the stack to be rebuilt; monkeypatch restores both afterwards.
    """
    from starlette.middleware.cors import CORSMiddleware

    from app.main import app

    cors = next(m for m in app.user_middleware if m.cls is CORSMiddleware)
    monkeypatch.setattr(cors, "kwargs", {**cors.kwargs, "allow_origins": [CORS_ORIGIN]})
    monkeypatch.setattr(app, "middleware_stack", None)
    return TestClient(app)


def _assert_forbidden(response) -> None:
    assert response.status_code == 403
    assert response.json() == {"detail": "forbidden"}
    body = response.text.lower()
    assert SECRET.lower() not in body
    assert settings.origin_auth_header.lower() not in body
    for value in response.headers.values():
        assert SECRET not in value
    # httpx headers are case-insensitive.
    assert settings.origin_auth_header not in response.headers


class TestOriginLockdown:
    def test_missing_header_rejected(self, origin_secret, real_client):
        _assert_forbidden(real_client.get("/openapi.json"))

    def test_wrong_header_rejected(self, origin_secret, real_client):
        response = real_client.get(
            "/openapi.json", headers={"X-Origin-Auth": "wrong-value"}
        )
        _assert_forbidden(response)

    def test_prefix_of_secret_rejected(self, origin_secret, real_client):
        response = real_client.get(
            "/openapi.json", headers={"X-Origin-Auth": SECRET[:-1]}
        )
        _assert_forbidden(response)

    def test_duplicate_header_rejected(self, origin_secret, real_client):
        response = real_client.get(
            "/openapi.json",
            headers=[("X-Origin-Auth", SECRET), ("X-Origin-Auth", SECRET)],
        )
        _assert_forbidden(response)

    def test_matching_header_allowed(self, origin_secret, real_client):
        response = real_client.get("/openapi.json", headers={"X-Origin-Auth": SECRET})
        assert response.status_code == 200

    def test_header_name_case_insensitive(self, origin_secret, real_client):
        response = real_client.get("/openapi.json", headers={"x-origin-auth": SECRET})
        assert response.status_code == 200

    def test_auth_routes_rejected_before_handler(self, origin_secret, real_client):
        _assert_forbidden(real_client.post("/auth/refresh", json={}))

    def test_health_exempt(self, origin_secret, real_client):
        response = real_client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_health_db_not_exempt(self, origin_secret, real_client):
        _assert_forbidden(real_client.get("/health/db"))

    def test_disabled_when_secret_empty(self, monkeypatch, real_client):
        monkeypatch.setattr(settings, "origin_auth_secret", "")
        assert real_client.get("/openapi.json").status_code == 200

    def test_forbidden_response_has_security_headers(self, origin_secret, real_client):
        response = real_client.get("/openapi.json")
        assert response.status_code == 403
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert "default-src 'none'" in response.headers["Content-Security-Policy"]
        assert "X-Request-ID" in response.headers

    def test_rejection_not_logged_with_secret_or_header(
        self, origin_secret, real_client, caplog
    ):
        with caplog.at_level(logging.DEBUG):
            real_client.get("/openapi.json", headers={"X-Origin-Auth": "wrong-value"})
        text = caplog.text.lower()
        assert "origin_auth_rejected" in text
        assert SECRET.lower() not in text
        assert "wrong-value" not in text
        assert "x-origin-auth" not in text

    @pytest.mark.parametrize(
        "path",
        [
            "/health/",
            # Full URL: the test client reads a bare "//health" as a host.
            "http://testserver//health",
            "/HEALTH",
            "/health%2F",
            "/health/db",
        ],
    )
    def test_only_exact_health_path_is_exempt(self, origin_secret, real_client, path):
        _assert_forbidden(real_client.get(path))

    @pytest.mark.parametrize("path", ["/health", "/health?x=1"])
    def test_exact_health_path_is_exempt(self, origin_secret, real_client, path):
        assert real_client.get(path).status_code == 200

    def test_empty_header_value_rejected(self, origin_secret, real_client):
        response = real_client.get("/openapi.json", headers={"X-Origin-Auth": ""})
        _assert_forbidden(response)

    def test_forbidden_response_carries_cors_for_allowed_origin(
        self, origin_secret, cors_client
    ):
        response = cors_client.get("/openapi.json", headers={"Origin": CORS_ORIGIN})
        _assert_forbidden(response)
        assert response.headers["access-control-allow-origin"] == CORS_ORIGIN

    def test_forbidden_response_has_no_cors_for_unknown_origin(
        self, origin_secret, cors_client
    ):
        response = cors_client.get(
            "/openapi.json", headers={"Origin": "https://not-allowed.example"}
        )
        _assert_forbidden(response)
        assert "access-control-allow-origin" not in response.headers

    def test_cross_origin_response_exposes_retry_after(self, cors_client):
        # Browsers hide Retry-After from scripts unless CORS exposes it; the
        # web client needs it for the scan 429s (daily_scan_limit,
        # scan_in_progress).
        response = cors_client.get("/health", headers={"Origin": CORS_ORIGIN})
        assert response.headers["access-control-allow-origin"] == CORS_ORIGIN
        assert response.headers["access-control-expose-headers"] == "Retry-After"

    def test_rejected_requests_do_not_consume_rate_limit(
        self, origin_secret, proxy_mode
    ):
        proxy_mode("none")
        client = TestClient(_limited_app(with_origin_auth=True))
        for _ in range(5):
            assert client.get("/limited").status_code == 403
        ok = client.get("/limited", headers={"X-Origin-Auth": SECRET})
        assert ok.status_code == 200


def test_resolver_module_is_single_source():
    """The limiter keys IPs only through app.core.client_ip."""
    import inspect

    from app.core import limiter as limiter_module

    source = inspect.getsource(limiter_module)
    assert "get_remote_address" not in source
    assert "client_ip_bucket" in source
    assert client_ip.CF_CONNECTING_IP == "cf-connecting-ip"

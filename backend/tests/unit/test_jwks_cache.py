"""Unit tests for JwksCache (#691) — fake HTTP client and injectable clock."""

import asyncio
import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from authlib.jose.errors import JoseError

from app.auth import google as google_mod
from app.auth.jwks_cache import (
    DEFAULT_TTL_SECONDS,
    MAX_STALE_SECONDS,
    MAX_TTL_SECONDS,
    MIN_REFRESH_INTERVAL_SECONDS,
    JwksCache,
    parse_max_age,
)

URL = "https://example.test/certs"


def _jwks(*kids):
    return {"keys": [{"kty": "RSA", "kid": k} for k in kids]}


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeHttp:
    """Patches httpx.AsyncClient; responses are consumed from a queue."""

    def __init__(self):
        self.calls = 0
        self.jwks = _jwks("k1")
        self.cache_control = "public, max-age=600"
        self.fail = False
        self.delay = False

    def client(self, *args, **kwargs):
        outer = self

        async def get(url):
            outer.calls += 1
            if outer.delay:
                await asyncio.sleep(0)
            if outer.fail:
                raise httpx.ConnectError("down")
            resp = MagicMock()
            resp.json.return_value = outer.jwks
            resp.headers = {"cache-control": outer.cache_control}
            resp.raise_for_status = MagicMock()
            return resp

        c = AsyncMock()
        c.get = get
        c.__aenter__ = AsyncMock(return_value=c)
        c.__aexit__ = AsyncMock(return_value=False)
        return c


@pytest.fixture
def http():
    fake = FakeHttp()
    with patch("app.auth.jwks_cache.httpx.AsyncClient", side_effect=fake.client):
        yield fake


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def cache(clock):
    return JwksCache(URL, clock=clock)


class TestParseMaxAge:
    def test_parses_max_age(self):
        assert parse_max_age("public, max-age=600, must-revalidate") == 600

    def test_fallback_when_missing(self):
        assert parse_max_age(None) == DEFAULT_TTL_SECONDS
        assert parse_max_age("no-cache") == DEFAULT_TTL_SECONDS

    def test_capped_at_24h(self):
        assert parse_max_age("max-age=999999999") == MAX_TTL_SECONDS

    def test_floored_at_throttle(self):
        assert parse_max_age("max-age=0") == MIN_REFRESH_INTERVAL_SECONDS


class TestCaching:
    async def test_many_calls_within_ttl_fetch_once(self, cache, http):
        for _ in range(5):
            assert await cache.get_jwks("k1") == _jwks("k1")
        assert http.calls == 1

    async def test_refetch_after_ttl_expiry(self, cache, http, clock):
        await cache.get_jwks("k1")
        clock.advance(601)
        await cache.get_jwks("k1")
        assert http.calls == 2

    async def test_fallback_ttl_when_no_header(self, cache, http, clock):
        http.cache_control = ""
        await cache.get_jwks()
        clock.advance(DEFAULT_TTL_SECONDS - 1)
        await cache.get_jwks()
        assert http.calls == 1
        clock.advance(2)
        await cache.get_jwks()
        assert http.calls == 2

    async def test_cap_applied_to_huge_max_age(self, cache, http, clock):
        http.cache_control = "max-age=99999999"
        await cache.get_jwks()
        clock.advance(MAX_TTL_SECONDS + 1)
        await cache.get_jwks()
        assert http.calls == 2

    async def test_concurrent_callers_share_one_fetch(self, cache, http):
        http.delay = True
        results = await asyncio.gather(*(cache.get_jwks("k1") for _ in range(10)))
        assert http.calls == 1
        assert all(r == _jwks("k1") for r in results)


class TestKeyRotation:
    async def test_unknown_kid_forces_one_refresh_and_is_accepted(
        self, cache, http, clock
    ):
        await cache.get_jwks("k1")
        http.jwks = _jwks("k1", "k2")
        clock.advance(MIN_REFRESH_INTERVAL_SECONDS + 1)
        assert await cache.get_jwks("k2") == _jwks("k1", "k2")
        assert await cache.get_jwks("k2") == _jwks("k1", "k2")
        assert http.calls == 2

    async def test_unknown_kid_refresh_is_throttled(self, cache, http, clock):
        await cache.get_jwks("k1")
        clock.advance(MIN_REFRESH_INTERVAL_SECONDS + 1)
        await cache.get_jwks("nope")
        assert http.calls == 2
        clock.advance(MIN_REFRESH_INTERVAL_SECONDS - 5)
        await cache.get_jwks("nope")
        await cache.get_jwks("nope")
        assert http.calls == 2
        clock.advance(10)
        await cache.get_jwks("nope")
        assert http.calls == 3

    async def test_unknown_kid_right_after_cold_fetch_is_throttled(self, cache, http):
        await cache.get_jwks("nope")
        await cache.get_jwks("nope")
        assert http.calls == 1

    async def test_concurrent_unknown_kid_single_refresh(self, cache, http, clock):
        await cache.get_jwks("k1")
        clock.advance(MIN_REFRESH_INTERVAL_SECONDS + 1)
        http.delay = True
        await asyncio.gather(*(cache.get_jwks("nope") for _ in range(8)))
        assert http.calls == 2


class TestStaleOnError:
    async def test_expired_cache_served_when_refresh_fails(self, cache, http, clock):
        await cache.get_jwks("k1")
        http.fail = True
        clock.advance(601)
        assert await cache.get_jwks("k1") == _jwks("k1")

    async def test_failed_refresh_retry_is_throttled(self, cache, http, clock):
        await cache.get_jwks("k1")
        http.fail = True
        clock.advance(601)
        await cache.get_jwks("k1")
        await cache.get_jwks("k1")
        assert http.calls == 2  # initial + one failed attempt
        clock.advance(MIN_REFRESH_INTERVAL_SECONDS + 1)
        await cache.get_jwks("k1")
        assert http.calls == 3

    async def test_fresh_cache_served_when_forced_refresh_fails(
        self, cache, http, clock
    ):
        await cache.get_jwks("k1")
        http.fail = True
        clock.advance(MIN_REFRESH_INTERVAL_SECONDS + 1)
        assert await cache.get_jwks("nope") == _jwks("k1")

    async def test_hard_staleness_limit(self, cache, http, clock):
        await cache.get_jwks("k1")
        http.fail = True
        clock.advance(600 + MAX_STALE_SECONDS + 1)
        with pytest.raises(httpx.ConnectError):
            await cache.get_jwks("k1")

    async def test_cold_cache_failure_propagates(self, cache, http):
        http.fail = True
        with pytest.raises(httpx.ConnectError):
            await cache.get_jwks("k1")

    async def test_warning_log_has_no_token_content(self, cache, http, clock, caplog):
        await cache.get_jwks("k1")
        http.fail = True
        clock.advance(601)
        with caplog.at_level("WARNING", logger="app.auth.jwks_cache"):
            await cache.get_jwks("secret.kid.value")
        assert "secret.kid.value" not in caplog.text
        assert "serving cached keys" in caplog.text


def _token_with_kid(kid):
    header = base64.urlsafe_b64encode(json.dumps({"kid": kid}).encode()).rstrip(b"=")
    return header.decode() + ".payload.sig"


class TestGoogleIntegration:
    def test_token_kid_extraction(self):
        assert google_mod._token_kid(_token_with_kid("abc")) == "abc"
        assert google_mod._token_kid("garbage") is None
        assert google_mod._token_kid("") is None

    async def test_signins_within_ttl_trigger_one_fetch(self, http):
        http.jwks = _jwks("k1")

        class C(dict):
            def validate(self):
                pass

        with (
            patch("app.auth.google.authlib_jwt.decode", return_value=C(sub="1")),
            patch("app.auth.google.settings") as s,
        ):
            s.google_client_ids = ["c"]
            for _ in range(5):
                await google_mod.verify_google_id_token(_token_with_kid("k1"))
        assert http.calls == 1

    async def test_unknown_kid_after_refresh_rejected_and_throttled(self, http):
        from authlib.jose import JsonWebKey, jwt

        key = JsonWebKey.generate_key(
            "RSA", 2048, is_private=True, options={"kid": "k1"}
        )
        http.jwks = {"keys": [key.as_dict(is_private=False)]}
        other = JsonWebKey.generate_key(
            "RSA", 2048, is_private=True, options={"kid": "other"}
        )
        token = jwt.encode(
            {"alg": "RS256", "kid": "other"},
            {"iss": "https://accounts.google.com", "aud": "c", "sub": "1"},
            other,
        ).decode()
        with patch("app.auth.google.settings") as s:
            s.google_client_ids = ["c"]
            for _ in range(3):
                with pytest.raises((ValueError, JoseError)):
                    await google_mod.verify_google_id_token(token)
        assert http.calls == 1

    async def test_warm_cache_survives_google_outage(self, http):
        from authlib.jose import JsonWebKey, jwt

        key = JsonWebKey.generate_key(
            "RSA", 2048, is_private=True, options={"kid": "k1"}
        )
        http.jwks = {"keys": [key.as_dict(is_private=False)]}
        token = jwt.encode(
            {"alg": "RS256", "kid": "k1"},
            {
                "iss": "https://accounts.google.com",
                "aud": "c",
                "sub": "1",
                "exp": 9999999999,
            },
            key,
        ).decode()
        with patch("app.auth.google.settings") as s:
            s.google_client_ids = ["c"]
            assert (await google_mod.verify_google_id_token(token))["sub"] == "1"
            http.fail = True
            assert (await google_mod.verify_google_id_token(token))["sub"] == "1"
        assert http.calls == 1

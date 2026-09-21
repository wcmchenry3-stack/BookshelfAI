"""Tests that `settings.is_hardened` (environment not in {development, test})
drives every security gate in app/main.py — not just literal "production".

A public `staging` deployment is just as attacker-reachable as production, so
it must get the exact same hardening (no /docs, no debug/test-only routes,
TrustedHostMiddleware active) even though Sentry still tags it "staging".

These gates are decided at module import time (docs_url, middleware
registration, route registration), so exercising them for a non-default
ENVIRONMENT requires actually reloading app.main + app.core.config with that
env var set. The `_reload_app` fixture below does that and restores the
original (development) modules afterward so later tests aren't affected.
"""

import os
import sys

import pytest
from httpx import ASGITransport, AsyncClient

_RELOADED_MODULES = ("app.main", "app.core.config")
_ENV_KEYS = ("ENVIRONMENT", "DATABASE_URL", "CORS_ORIGINS", "TRUSTED_HOSTS")

# `app.core.limiter.limiter` is a process-wide singleton that is deliberately
# NOT reloaded (see _RELOADED_MODULES). slowapi's `@limiter.limit(...)`
# decorator keys its bookkeeping by f"{func.__module__}.{func.__name__}" and
# *appends* to that key's limit list on every decoration
# (Limiter._route_limits.setdefault(name, []).extend(...)). Reloading
# app.main re-decorates functions with the same qualified names (e.g.
# "app.main.test_login"), so each reload permanently duplicates that route's
# registered limits on the shared singleton — silently lowering the effective
# rate limit for every other test in the session. We snapshot and restore the
# limiter's internal dicts around each reload to prevent that leak.
_LIMITER_ATTRS = (
    "_route_limits",
    "_dynamic_route_limits",
    "_Limiter__marked_for_limiting",
)


def _snapshot_limiter():
    from app.core.limiter import limiter

    return {
        attr: {k: list(v) for k, v in getattr(limiter, attr).items()}
        for attr in _LIMITER_ATTRS
    }


def _restore_limiter(snapshot):
    from app.core.limiter import limiter

    for attr, value in snapshot.items():
        getattr(limiter, attr).clear()
        getattr(limiter, attr).update(value)


def _reload_app(**env_overrides):
    for key, value in env_overrides.items():
        os.environ[key] = value
    for mod_name in _RELOADED_MODULES:
        sys.modules.pop(mod_name, None)
    import app.main as main_module

    return main_module


@pytest.fixture
def reload_app():
    """Yields a function that reloads app.main under a temporary environment.

    Restores the original modules (rebuilt from the original env vars) and the
    shared limiter's bookkeeping on teardown, so later tests see the normal
    development configuration and rate limits again.
    """
    original_env = {k: os.environ.get(k) for k in _ENV_KEYS}
    limiter_snapshot = _snapshot_limiter()

    yield _reload_app

    for key, value in original_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    for mod_name in _RELOADED_MODULES:
        sys.modules.pop(mod_name, None)
    import app.main  # noqa: F401 — restore the module for later tests

    _restore_limiter(limiter_snapshot)


@pytest.mark.asyncio
async def test_staging_is_fully_hardened(reload_app):
    main_module = reload_app(
        ENVIRONMENT="staging",
        DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
        CORS_ORIGINS="[]",
        TRUSTED_HOSTS='["trusted.example.com"]',
    )
    assert main_module.settings.is_hardened is True

    route_paths = {getattr(route, "path", None) for route in main_module.app.routes}
    assert "/debug/sentry-test" not in route_paths
    assert "/auth/test-login" not in route_paths

    async with AsyncClient(
        transport=ASGITransport(app=main_module.app),
        base_url="http://trusted.example.com",
    ) as client:
        assert (await client.get("/docs")).status_code == 404
        assert (await client.post("/auth/test-login", json={})).status_code == 404

        # TrustedHostMiddleware is active in staging: a non-exempt path from an
        # untrusted Host is rejected, while /health (the only exempt path) works.
        assert (await client.get("/health")).status_code == 200

    async with AsyncClient(
        transport=ASGITransport(app=main_module.app),
        base_url="http://attacker.example.com",
    ) as client:
        assert (await client.get("/health/db")).status_code == 400
        assert (await client.get("/health")).status_code == 200


@pytest.mark.asyncio
async def test_development_is_unchanged(reload_app):
    """Sanity check: development keeps its original (non-hardened) behaviour."""
    main_module = reload_app(
        ENVIRONMENT="development",
        DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
        CORS_ORIGINS="[]",
        TRUSTED_HOSTS='["trusted.example.com"]',
    )
    assert main_module.settings.is_hardened is False

    route_paths = {getattr(route, "path", None) for route in main_module.app.routes}
    assert "/debug/sentry-test" in route_paths
    assert "/auth/test-login" in route_paths

    async with AsyncClient(
        transport=ASGITransport(app=main_module.app),
        base_url="http://attacker.example.com",
    ) as client:
        assert (await client.get("/docs")).status_code == 200
        # Registered (not 404) — rejected for lacking TEST_AUTH_SECRET instead.
        resp = await client.post("/auth/test-login", json={})
        assert resp.status_code == 403
        # No TrustedHostMiddleware: an arbitrary Host header is accepted everywhere.
        assert (await client.get("/health/db")).status_code in (200, 503)

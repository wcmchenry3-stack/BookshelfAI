"""Tests the /health exemption from TrustedHostMiddleware.

/health is legitimately hit by CI probes, uptime monitors, and load balancers
using whatever hostname the origin is reachable at (e.g. the raw Render
`*.onrender.com` URL), which won't be in the production `trusted_hosts`
allowlist. Without the exemption, those probes get 400 before reaching the
handler.

/health/db is deliberately NOT exempt — at the raw origin an attacker could
rotate a spoofed CF-Connecting-IP header per request to dodge its rate limit
and exhaust the DB pool, so it must go through the same Host enforcement as
every other route.
"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


def _build_app():
    from app.main import _HealthExemptTrustedHost

    app = FastAPI()
    app.add_middleware(_HealthExemptTrustedHost, allowed_hosts=["trusted.example.com"])

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/health/db")
    async def health_db() -> dict:
        return {"status": "ok", "db": "ok"}

    @app.get("/other")
    async def other() -> dict:
        return {"status": "other"}

    return app


@pytest.mark.asyncio
async def test_health_accepts_untrusted_host_header():
    """Probes from raw origin hostnames must still hit /health."""
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()),
        base_url="http://bookshelf-api-rxp3.onrender.com",
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_non_health_path_still_rejects_untrusted_host():
    """Every non-/health path still enforces the trusted_hosts allowlist."""
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()),
        base_url="http://attacker.example.com",
    ) as client:
        response = await client.get("/other")

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_non_health_path_passes_for_trusted_host():
    """Sanity: trusted hosts still pass through normally on non-/health paths."""
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()),
        base_url="http://trusted.example.com",
    ) as client:
        response = await client.get("/other")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_health_db_rejects_untrusted_host_while_health_still_allowed():
    """/health/db is not exempt: an untrusted Host header must be rejected (400)
    for it even though the same request would succeed against plain /health."""
    app = _build_app()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://bookshelf-api-rxp3.onrender.com",
    ) as client:
        health_response = await client.get("/health")
        health_db_response = await client.get("/health/db")

    assert health_response.status_code == 200
    assert health_db_response.status_code == 400

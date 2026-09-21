"""Unit tests for the /health endpoint."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def app():
    from app.main import app

    return app


@pytest.mark.asyncio
async def test_health_ok(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"


@pytest.mark.asyncio
async def test_health_ignores_db_errors(app):
    """/health is a pure liveness probe — it must return 200 even when the
    database is unreachable, since Render restarts the service on a failed
    health check. DB connectivity is checked separately at /health/db."""
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(side_effect=Exception("connection refused"))

    with patch("app.main.AsyncSessionLocal", return_value=mock_cm):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"

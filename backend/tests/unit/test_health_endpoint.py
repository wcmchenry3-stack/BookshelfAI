"""Unit tests for the /health endpoint."""

import asyncio
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

    with patch(
        "app.main.AsyncSessionLocal", return_value=mock_cm
    ) as mock_session_local:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    # /health must never touch the DB at all — not just tolerate a failure.
    mock_session_local.assert_not_called()


@pytest.mark.asyncio
async def test_health_db_times_out_returns_503(app, monkeypatch):
    """/health/db must not hang forever on a stuck DB connection."""

    async def _hangs(*args, **kwargs):
        await asyncio.sleep(10)

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=_hangs)
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    monkeypatch.setattr("app.main._HEALTH_DB_TIMEOUT_SECONDS", 0.05)

    with patch("app.main.AsyncSessionLocal", return_value=mock_cm):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/health/db")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "error"
    assert body["db"] == "error"

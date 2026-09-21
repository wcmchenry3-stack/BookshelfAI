"""Integration tests for the /health/db endpoint (DB connectivity check)."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def app():
    from app.main import app

    return app


@pytest.mark.asyncio
async def test_health_db_ok(app):
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock()
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("app.main.AsyncSessionLocal", return_value=mock_cm):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/health/db")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"


@pytest.mark.asyncio
async def test_health_db_error_returns_503(app):
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(side_effect=Exception("connection refused"))

    with patch("app.main.AsyncSessionLocal", return_value=mock_cm):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/health/db")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "error"
    assert body["db"] == "error"

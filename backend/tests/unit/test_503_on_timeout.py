"""Unit tests for the QueryCanceledError -> 503 handler (#696).

Verifies that a statement_timeout cancellation from asyncpg is mapped to
HTTP 503 with the correct body and Retry-After header, without leaking any
SQL parameters or user data in the log output.

Strategy: The /health/db endpoint catches all exceptions itself, so we test
the _query_canceled_handler function directly and via a synthetic endpoint
that raises QueryCanceledError without swallowing it.
"""

import json
import logging

import pytest

pytestmark = pytest.mark.asyncio


def test_query_canceled_handler_returns_503():
    """_query_canceled_handler returns 503 with the correct body."""
    try:
        import asyncpg.exceptions as _asyncpg_exc
    except ImportError:
        pytest.skip("asyncpg not installed")

    from unittest.mock import MagicMock

    from app.main import _TIMEOUT_RETRY_AFTER, _query_canceled_handler

    mock_request = MagicMock()
    mock_request.url.path = "/scan"

    exc = _asyncpg_exc.QueryCanceledError()
    response = _query_canceled_handler(mock_request, exc)

    assert response.status_code == 503
    body = json.loads(response.body)
    assert body["error"]["code"] == "SERVICE_TEMPORARILY_UNAVAILABLE"
    assert body["retry_after"] == _TIMEOUT_RETRY_AFTER
    assert response.headers.get("retry-after") == str(_TIMEOUT_RETRY_AFTER)


def test_query_canceled_handler_logs_path_only(caplog):
    """The 503 handler logs the route path only, no SQL or user data."""
    try:
        import asyncpg.exceptions as _asyncpg_exc
    except ImportError:
        pytest.skip("asyncpg not installed")

    from unittest.mock import MagicMock

    from app.main import _query_canceled_handler

    mock_request = MagicMock()
    mock_request.url.path = "/user-books"

    exc = _asyncpg_exc.QueryCanceledError()

    with caplog.at_level(logging.WARNING, logger="app.main"):
        _query_canceled_handler(mock_request, exc)

    timeout_records = [
        r for r in caplog.records if "statement_timeout_canceled" in r.message
    ]
    assert timeout_records, "expected a statement_timeout_canceled log record"
    assert "/user-books" in timeout_records[0].message
    for record in timeout_records:
        assert "SELECT" not in record.message.upper()
        assert "INSERT" not in record.message.upper()


async def test_query_canceled_error_triggers_503_via_http(app_with_probe):
    """QueryCanceledError raised in a route triggers HTTP 503."""
    try:
        import asyncpg.exceptions as _asyncpg_exc  # noqa: F401
    except ImportError:
        pytest.skip("asyncpg not installed")

    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app_with_probe), base_url="http://test"
    ) as client:
        response = await client.get("/probe/timeout")

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "SERVICE_TEMPORARILY_UNAVAILABLE"
    assert body["retry_after"] == 5
    assert response.headers.get("retry-after") == "5"


@pytest.fixture
def app_with_probe():
    """FastAPI app with an extra /probe/timeout route for testing."""
    try:
        import asyncpg.exceptions as _asyncpg_exc
    except ImportError:
        pytest.skip("asyncpg not installed")

    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from slowapi import _rate_limit_exceeded_handler as _slowapi_429
    from slowapi.errors import RateLimitExceeded

    from app.main import (
        _query_canceled_handler,
        _QueryCanceledError,
        limiter,
        settings,
    )

    probe_app = FastAPI()
    probe_app.state.limiter = limiter
    probe_app.add_exception_handler(RateLimitExceeded, _slowapi_429)
    if _QueryCanceledError is not None:
        probe_app.add_exception_handler(_QueryCanceledError, _query_canceled_handler)

    @probe_app.get("/probe/timeout")
    @limiter.limit(settings.rate_limit_reads)
    async def probe_timeout(request: Request) -> JSONResponse:
        raise _asyncpg_exc.QueryCanceledError()

    return probe_app


def test_query_canceled_handler_is_registered():
    """_query_canceled_handler is registered on the real app."""
    try:
        import asyncpg.exceptions as _asyncpg_exc
    except ImportError:
        pytest.skip("asyncpg not installed")

    from app.main import app

    exc_handlers = getattr(app, "exception_handlers", {})
    assert _asyncpg_exc.QueryCanceledError in exc_handlers, (
        "QueryCanceledError handler not found in app.exception_handlers; "
        "check _query_canceled_handler registration in main.py"
    )

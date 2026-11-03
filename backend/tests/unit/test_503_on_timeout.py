"""Unit tests for the statement_timeout -> 503 handler (#696).

Through SQLAlchemy a statement_timeout does not surface as asyncpg's
``QueryCanceledError``: the asyncpg adapter translates it into its own
``Error`` (carrying ``sqlstate == "57014"``, with the asyncpg error as
``__cause__``) and SQLAlchemy wraps that in ``sqlalchemy.exc.DBAPIError``.
These tests build the exception through that same translation path, so they
fail if the handler only matches the raw driver error.

The real-Postgres path (pg_sleep under a tiny timeout through ``get_db``) is
covered by tests/integration/test_statement_timeout_db.py.
"""

import json
import logging
from unittest.mock import MagicMock

import asyncpg
import pytest
from fastapi import Depends, FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects.postgresql.asyncpg import (
    AsyncAdapt_asyncpg_connection,
    AsyncAdapt_asyncpg_dbapi,
)
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.main import (
    _TIMEOUT_RETRY_AFTER,
    _is_query_canceled,
    _query_canceled_handler,
    app,
    limiter,
    settings,
)

_STATEMENT = "SELECT * FROM users WHERE email = $1"
_PARAMS = ("someone@example.com",)


def _sqlalchemy_wrapped(driver_error: Exception) -> DBAPIError:
    """Wrap a raw asyncpg error exactly as SQLAlchemy's asyncpg dialect does."""
    dbapi = AsyncAdapt_asyncpg_dbapi(asyncpg)
    try:
        AsyncAdapt_asyncpg_connection._handle_exception_no_connection(
            dbapi, driver_error
        )
    except dbapi.Error as adapted:
        return DBAPIError.instance(
            _STATEMENT, _PARAMS, adapted, dbapi.Error, hide_parameters=True
        )
    raise AssertionError("adapter did not translate the driver error")


def _timeout_error() -> DBAPIError:
    return _sqlalchemy_wrapped(
        asyncpg.exceptions.QueryCanceledError(
            "canceling statement due to statement timeout"
        )
    )


def _unique_violation() -> DBAPIError:
    return _sqlalchemy_wrapped(
        asyncpg.exceptions.UniqueViolationError("duplicate key value")
    )


def _request(path: str) -> MagicMock:
    request = MagicMock()
    request.url.path = path
    return request


# --- the wrapped shape this handler must recognise ---------------------------


def test_wrapped_timeout_has_the_shape_seen_on_real_postgres():
    exc = _timeout_error()
    assert type(exc) is DBAPIError
    assert exc.orig.sqlstate == "57014"
    assert isinstance(exc.orig.__cause__, asyncpg.exceptions.QueryCanceledError)


def test_is_query_canceled_true_for_wrapped_timeout():
    assert _is_query_canceled(_timeout_error()) is True


def test_is_query_canceled_true_for_raw_driver_error():
    assert _is_query_canceled(asyncpg.exceptions.QueryCanceledError("x")) is True


def test_is_query_canceled_true_for_sqlstate_only():
    """A driver error recognised by SQLSTATE alone (no asyncpg class)."""

    class _Orig(Exception):
        sqlstate = "57014"

    exc = DBAPIError("SELECT 1", None, _Orig("cancel"))
    assert _is_query_canceled(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        _unique_violation(),
        DBAPIError("SELECT 1", None, Exception("no sqlstate")),
        ValueError("not a db error"),
    ],
)
def test_is_query_canceled_false_for_other_errors(exc):
    assert _is_query_canceled(exc) is False


def test_is_query_canceled_stops_on_a_cycle():
    a = Exception("a")
    b = Exception("b")
    a.__cause__ = b
    b.__cause__ = a
    assert _is_query_canceled(a) is False


# --- the handler -------------------------------------------------------------


def test_handler_returns_503_for_wrapped_timeout():
    response = _query_canceled_handler(_request("/scan"), _timeout_error())

    assert response.status_code == 503
    body = json.loads(response.body)
    assert body == {
        "error": {
            "code": "SERVICE_TEMPORARILY_UNAVAILABLE",
            "message": "The server is busy, please retry shortly.",
        },
        "retry_after": _TIMEOUT_RETRY_AFTER,
    }
    assert response.headers["retry-after"] == str(_TIMEOUT_RETRY_AFTER)


def test_handler_reraises_other_db_errors():
    exc = _unique_violation()
    with pytest.raises(IntegrityError) as exc_info:
        _query_canceled_handler(_request("/user-books"), exc)
    assert exc_info.value is exc


def test_handler_logs_path_only(caplog):
    with caplog.at_level(logging.WARNING, logger="app.main"):
        _query_canceled_handler(_request("/user-books"), _timeout_error())

    records = [r for r in caplog.records if "statement_timeout_canceled" in r.message]
    assert len(records) == 1
    assert records[0].getMessage() == "statement_timeout_canceled path=/user-books"
    assert records[0].exc_info is None


def test_handler_is_registered_for_dbapi_error_on_the_real_app():
    assert app.exception_handlers.get(DBAPIError) is _query_canceled_handler


# --- through HTTP, with the real app's exception handlers --------------------


@pytest.fixture
def probe_app() -> FastAPI:
    """An app carrying the real app's exception handlers plus probe routes that
    raise what a DB session raises."""
    probe = FastAPI()
    probe.state.limiter = limiter
    for exc_class, handler in app.exception_handlers.items():
        probe.add_exception_handler(exc_class, handler)

    async def _session():
        yield None

    @probe.get("/probe/timeout")
    @limiter.limit(settings.rate_limit_reads)
    async def probe_timeout(request: Request, _db=Depends(_session)):
        raise _timeout_error()

    @probe.get("/probe/integrity")
    @limiter.limit(settings.rate_limit_reads)
    async def probe_integrity(request: Request, _db=Depends(_session)):
        raise _unique_violation()

    return probe


async def test_wrapped_timeout_returns_503_over_http(probe_app):
    async with AsyncClient(
        transport=ASGITransport(app=probe_app), base_url="http://test"
    ) as client:
        response = await client.get("/probe/timeout")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_TEMPORARILY_UNAVAILABLE"
    assert response.json()["retry_after"] == _TIMEOUT_RETRY_AFTER
    assert response.headers["retry-after"] == str(_TIMEOUT_RETRY_AFTER)


async def test_other_db_error_keeps_the_500_path_over_http(probe_app):
    """A non-timeout DBAPIError is not turned into 503: it propagates as an
    unhandled error (500 from ServerErrorMiddleware, reported to Sentry)."""
    async with AsyncClient(
        transport=ASGITransport(app=probe_app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get("/probe/integrity")

    assert response.status_code == 500
    assert "retry-after" not in response.headers


async def test_other_db_error_propagates_unchanged(probe_app):
    async with AsyncClient(
        transport=ASGITransport(app=probe_app), base_url="http://test"
    ) as client:
        with pytest.raises(IntegrityError):
            await client.get("/probe/integrity")

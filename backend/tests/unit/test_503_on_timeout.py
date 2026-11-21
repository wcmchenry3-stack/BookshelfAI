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

from app import main as main_module
from app.main import (
    _TIMEOUT_RETRY_AFTER,
    DB_TIMEOUT_DETAIL,
    _is_query_canceled,
    _query_canceled_handler,
    app,
    limiter,
    settings,
)
from tests.unit._sentry_capture import captured_app_events

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


def _request(path: str, template: str | None = None) -> MagicMock:
    request = MagicMock()
    request.url.path = path
    route = MagicMock()
    route.path = template or path
    request.scope = {"route": route}
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
    # The API's standard error shape (docs/api.md), which the frontend reads.
    assert body == {"detail": "db_timeout"}
    assert DB_TIMEOUT_DETAIL == "db_timeout"
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
    assert response.json() == {"detail": "db_timeout"}
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


# --- Sentry: one warning per cancelled query ---------------------------------


@pytest.fixture
def probe_app_with_ids(probe_app) -> FastAPI:
    @probe_app.get("/probe/users/{user_id}/timeout")
    @limiter.limit(settings.rate_limit_reads)
    async def probe_user_timeout(request: Request, user_id: str):
        raise _timeout_error()

    return probe_app


async def test_timeout_sends_one_scrubbed_sentry_warning(probe_app_with_ids):
    """The 503 path captures exactly one warning event, grouped by a fixed
    fingerprint and tagged with the route template: no concrete path, ids,
    SQL, parameters or request data (real SDK client, app integrations)."""
    secret_id = "a2c4e6f8-1111-2222-3333-444455556666"
    with captured_app_events() as events:
        async with AsyncClient(
            transport=ASGITransport(app=probe_app_with_ids), base_url="http://test"
        ) as client:
            response = await client.get(
                f"/probe/users/{secret_id}/timeout",
                headers={"Authorization": "Bearer not-a-real-token"},
            )

    assert response.status_code == 503
    assert len(events) == 1
    event = events[0]
    assert event["level"] == "warning"
    assert event["message"] == "database statement timeout"
    assert event["fingerprint"] == ["db-statement-timeout"]
    assert event["tags"]["route"] == "/probe/users/{user_id}/timeout"
    blob = json.dumps(event)
    for forbidden in (secret_id, "not-a-real-token", _STATEMENT, _PARAMS[0]):
        assert forbidden not in blob
    assert "request" not in event and "user" not in event


async def test_other_db_error_is_not_reported_as_a_timeout(probe_app):
    with captured_app_events() as events:
        async with AsyncClient(
            transport=ASGITransport(app=probe_app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            await client.get("/probe/integrity")

    assert all(e.get("fingerprint") != ["db-statement-timeout"] for e in events)


def test_sentry_failure_does_not_break_the_503(monkeypatch, caplog):
    def boom(*_a, **_k):
        raise RuntimeError("sentry down")

    monkeypatch.setattr(main_module, "capture_service_warning", boom)
    with caplog.at_level(logging.ERROR, logger="app.main"):
        response = _query_canceled_handler(_request("/scan"), _timeout_error())

    assert response.status_code == 503
    assert "statement_timeout_report_failed" in caplog.text


def test_route_template_falls_back_when_no_route_matched():
    request = MagicMock()
    request.scope = {}
    assert main_module._route_template(request) == "unmatched"

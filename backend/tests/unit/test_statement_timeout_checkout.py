"""Unit tests for how the app engine applies ``statement_timeout`` (#696).

Request connections get two layers: ``_statement_timeout_connect_args`` sets it as an asyncpg startup
parameter (``server_settings``), and ``_set_statement_timeout`` runs
``SET statement_timeout`` on every pool checkout. Both are skipped on SQLite
or when the setting is 0/negative. Background-job sessions
(``JobSessionLocal``) run ``SET LOCAL statement_timeout`` to the longer job
value at the start of every transaction. Also covers the startup
``current_user`` log. The DBAPI connection is a mock here; the
real-Postgres behaviour is covered by
tests/integration/test_statement_timeout_db.py.
"""

from unittest.mock import MagicMock

import pytest
from sqlalchemy import event

from app.core import database
from app.core.database import (
    JobSessionLocal,
    _JobSession,
    _set_job_statement_timeout,
    _set_statement_timeout,
    _statement_timeout_connect_args,
    engine,
    log_db_role,
)


def _dbapi_conn() -> tuple[MagicMock, MagicMock]:
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    return conn, cursor


def test_sets_timeout_on_postgres(monkeypatch):
    monkeypatch.setattr(database, "_is_sqlite", False)
    monkeypatch.setattr(database.settings, "statement_timeout_ms", 5000)
    conn, cursor = _dbapi_conn()

    _set_statement_timeout(conn, MagicMock(), MagicMock())

    cursor.execute.assert_called_once_with("SET statement_timeout = 5000")
    cursor.close.assert_called_once_with()


def test_uses_configured_value(monkeypatch):
    monkeypatch.setattr(database, "_is_sqlite", False)
    monkeypatch.setattr(database.settings, "statement_timeout_ms", 1234)
    conn, cursor = _dbapi_conn()

    _set_statement_timeout(conn, MagicMock(), MagicMock())

    cursor.execute.assert_called_once_with("SET statement_timeout = 1234")


@pytest.mark.parametrize("ms", [0, -1])
def test_disabled_when_not_positive(monkeypatch, ms):
    monkeypatch.setattr(database, "_is_sqlite", False)
    monkeypatch.setattr(database.settings, "statement_timeout_ms", ms)
    conn, _ = _dbapi_conn()

    _set_statement_timeout(conn, MagicMock(), MagicMock())

    conn.cursor.assert_not_called()


def test_skipped_on_sqlite(monkeypatch):
    monkeypatch.setattr(database, "_is_sqlite", True)
    monkeypatch.setattr(database.settings, "statement_timeout_ms", 5000)
    conn, _ = _dbapi_conn()

    _set_statement_timeout(conn, MagicMock(), MagicMock())

    conn.cursor.assert_not_called()


def test_listener_registered_on_engine_checkout():
    assert event.contains(engine.sync_engine, "checkout", _set_statement_timeout)


def test_listener_accepts_the_pool_checkout_signature(monkeypatch):
    """The pool dispatches checkout as (dbapi_conn, record, proxy)."""
    monkeypatch.setattr(database, "_is_sqlite", False)
    monkeypatch.setattr(database.settings, "statement_timeout_ms", 5000)
    conn, cursor = _dbapi_conn()

    engine.sync_engine.pool.dispatch.checkout(conn, MagicMock(), MagicMock())

    cursor.execute.assert_called_once_with("SET statement_timeout = 5000")


def test_connect_args_set_startup_parameter(monkeypatch):
    monkeypatch.setattr(database, "_is_sqlite", False)
    monkeypatch.setattr(database.settings, "statement_timeout_ms", 5000)

    assert _statement_timeout_connect_args() == {
        "server_settings": {"statement_timeout": "5000"}
    }


@pytest.mark.parametrize("ms", [0, -1])
def test_connect_args_empty_when_disabled(monkeypatch, ms):
    monkeypatch.setattr(database, "_is_sqlite", False)
    monkeypatch.setattr(database.settings, "statement_timeout_ms", ms)

    assert _statement_timeout_connect_args() == {}


def test_connect_args_empty_on_sqlite(monkeypatch):
    monkeypatch.setattr(database, "_is_sqlite", True)
    monkeypatch.setattr(database.settings, "statement_timeout_ms", 5000)

    assert _statement_timeout_connect_args() == {}


# --- background-job sessions -------------------------------------------------


def test_job_timeout_set_local_per_transaction(monkeypatch):
    monkeypatch.setattr(database, "_is_sqlite", False)
    monkeypatch.setattr(database.settings, "statement_timeout_job_ms", 60000)
    connection = MagicMock()

    _set_job_statement_timeout(MagicMock(), MagicMock(), connection)

    connection.exec_driver_sql.assert_called_once_with(
        "SET LOCAL statement_timeout = 60000"
    )


@pytest.mark.parametrize("ms", [0, -5])
def test_job_timeout_zero_or_negative_disables_it_for_the_job(monkeypatch, ms):
    monkeypatch.setattr(database, "_is_sqlite", False)
    monkeypatch.setattr(database.settings, "statement_timeout_job_ms", ms)
    connection = MagicMock()

    _set_job_statement_timeout(MagicMock(), MagicMock(), connection)

    connection.exec_driver_sql.assert_called_once_with(
        "SET LOCAL statement_timeout = 0"
    )


def test_job_timeout_skipped_on_sqlite(monkeypatch):
    monkeypatch.setattr(database, "_is_sqlite", True)
    connection = MagicMock()

    _set_job_statement_timeout(MagicMock(), MagicMock(), connection)

    connection.exec_driver_sql.assert_not_called()


def test_job_sessions_use_the_job_session_class():
    assert JobSessionLocal.kw["sync_session_class"] is _JobSession
    assert JobSessionLocal.kw["bind"] is engine
    assert event.contains(_JobSession, "after_begin", _set_job_statement_timeout)


def test_request_sessions_do_not_get_the_job_timeout():
    session = database.AsyncSessionLocal()
    assert not isinstance(session.sync_session, _JobSession)


def test_purge_jobs_open_job_sessions():
    """Every daily purge step in token_cleanup opens a JobSessionLocal session."""
    import inspect

    from app.services import token_cleanup

    source = inspect.getsource(token_cleanup)
    assert "AsyncSessionLocal" not in source
    assert source.count("async with JobSessionLocal() as db:") == 7


# --- startup role log ----------------------------------------------------------


class _FakeSession:
    def __init__(self, role=None, error=None):
        self._role = role
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _stmt):
        if self._error:
            raise self._error
        result = MagicMock()
        result.scalar_one.return_value = self._role
        return result


async def test_log_db_role_logs_the_app_role_at_info(monkeypatch, caplog):
    monkeypatch.setattr(database, "_is_sqlite", False)
    monkeypatch.setattr(database.settings, "environment", "production")
    monkeypatch.setattr(
        database, "AsyncSessionLocal", lambda: _FakeSession(role="bookshelf_app")
    )
    with caplog.at_level("INFO", logger="app.core.database"):
        assert await log_db_role() == "bookshelf_app"

    [record] = [r for r in caplog.records if "db_role" in r.message]
    assert record.levelname == "INFO"
    assert record.getMessage() == "db_role current_user=bookshelf_app"


async def test_log_db_role_warns_when_hardened_and_not_the_app_role(
    monkeypatch, caplog
):
    monkeypatch.setattr(database, "_is_sqlite", False)
    monkeypatch.setattr(database.settings, "environment", "production")
    monkeypatch.setattr(
        database, "AsyncSessionLocal", lambda: _FakeSession(role="postgres")
    )
    with caplog.at_level("INFO", logger="app.core.database"):
        assert await log_db_role() == "postgres"

    [record] = [r for r in caplog.records if "db_role" in r.message]
    assert record.levelname == "WARNING"
    assert record.getMessage() == "db_role current_user=postgres expected=bookshelf_app"


async def test_log_db_role_info_for_any_role_in_development(monkeypatch, caplog):
    monkeypatch.setattr(database, "_is_sqlite", False)
    monkeypatch.setattr(database.settings, "environment", "development")
    monkeypatch.setattr(database, "AsyncSessionLocal", lambda: _FakeSession(role="me"))
    with caplog.at_level("INFO", logger="app.core.database"):
        assert await log_db_role() == "me"

    [record] = [r for r in caplog.records if "db_role" in r.message]
    assert record.levelname == "INFO"


async def test_log_db_role_never_raises(monkeypatch, caplog):
    monkeypatch.setattr(database, "_is_sqlite", False)
    monkeypatch.setattr(
        database,
        "AsyncSessionLocal",
        lambda: _FakeSession(error=OSError("postgresql://u:secret@h/db")),
    )
    with caplog.at_level("INFO", logger="app.core.database"):
        assert await log_db_role() is None

    assert "db_role_check_failed error=OSError" in caplog.text
    assert "secret" not in caplog.text


async def test_log_db_role_skipped_on_sqlite(monkeypatch):
    monkeypatch.setattr(database, "_is_sqlite", True)
    monkeypatch.setattr(
        database, "AsyncSessionLocal", MagicMock(side_effect=AssertionError)
    )
    assert await log_db_role() is None


def test_lifespan_calls_log_db_role():
    import inspect

    from app import main

    assert "await log_db_role()" in inspect.getsource(main.lifespan)

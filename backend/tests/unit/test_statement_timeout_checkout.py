"""Unit tests for the pool-checkout ``statement_timeout`` listener (#696).

``_set_statement_timeout`` runs ``SET statement_timeout`` on every connection
the pool hands out, except on SQLite or when the setting is 0/negative. The
DBAPI connection is a mock here; the real-Postgres behaviour is covered by
tests/integration/test_db_role.py.
"""

from unittest.mock import MagicMock

import pytest
from sqlalchemy import event

from app.core import database
from app.core.database import _set_statement_timeout, engine

_PG_URL = "postgresql://u:p@db.example:5432/app"


def _dbapi_conn() -> tuple[MagicMock, MagicMock]:
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    return conn, cursor


def test_sets_timeout_on_postgres(monkeypatch):
    monkeypatch.setattr(database.settings, "database_url", _PG_URL)
    monkeypatch.setattr(database.settings, "statement_timeout_ms", 5000)
    conn, cursor = _dbapi_conn()

    _set_statement_timeout(conn, MagicMock(), MagicMock())

    cursor.execute.assert_called_once_with("SET statement_timeout = 5000")
    cursor.close.assert_called_once_with()


def test_uses_configured_value(monkeypatch):
    monkeypatch.setattr(database.settings, "database_url", _PG_URL)
    monkeypatch.setattr(database.settings, "statement_timeout_ms", 1234)
    conn, cursor = _dbapi_conn()

    _set_statement_timeout(conn, MagicMock(), MagicMock())

    cursor.execute.assert_called_once_with("SET statement_timeout = 1234")


@pytest.mark.parametrize("ms", [0, -1])
def test_disabled_when_not_positive(monkeypatch, ms):
    monkeypatch.setattr(database.settings, "database_url", _PG_URL)
    monkeypatch.setattr(database.settings, "statement_timeout_ms", ms)
    conn, _ = _dbapi_conn()

    _set_statement_timeout(conn, MagicMock(), MagicMock())

    conn.cursor.assert_not_called()


def test_skipped_on_sqlite(monkeypatch):
    monkeypatch.setattr(database.settings, "database_url", "sqlite:///./x.db")
    monkeypatch.setattr(database.settings, "statement_timeout_ms", 5000)
    conn, _ = _dbapi_conn()

    _set_statement_timeout(conn, MagicMock(), MagicMock())

    conn.cursor.assert_not_called()


def test_listener_registered_on_engine_checkout():
    assert event.contains(engine.sync_engine, "checkout", _set_statement_timeout)


def test_listener_accepts_the_pool_checkout_signature(monkeypatch):
    """The pool dispatches checkout as (dbapi_conn, record, proxy)."""
    monkeypatch.setattr(database.settings, "database_url", _PG_URL)
    monkeypatch.setattr(database.settings, "statement_timeout_ms", 5000)
    conn, cursor = _dbapi_conn()

    engine.sync_engine.pool.dispatch.checkout(conn, MagicMock(), MagicMock())

    cursor.execute.assert_called_once_with("SET statement_timeout = 5000")

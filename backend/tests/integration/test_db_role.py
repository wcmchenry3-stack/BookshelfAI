"""Integration tests for the bookshelf_app DB role (#696).

Tests:
  1. SELECT works under bookshelf_app.
  2. DROP TABLE / ALTER TABLE fails (DDL denied).
  3. A query that exceeds statement_timeout raises QueryCanceledError (or a
     SQLAlchemy wrapper with QueryCanceledError in its cause chain).

All schema changes run in a transaction that is rolled back, so the DB state is
unchanged after the test run. Role creation is also transactional (Postgres
rolls back CREATE ROLE inside a transaction).

These tests require a real PostgreSQL instance with:
  - alembic upgrade head already applied (the alembic_version table present)
  - The DATABASE_URL environment variable set to a non-dummy URL

They are skipped automatically when no real DB is configured (see conftest.py).
"""

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings

pytestmark = pytest.mark.integration

_APP_ROLE = "bookshelf_app"


def _role_exists(connection, role: str) -> bool:
    return bool(
        connection.execute(
            sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
        ).scalar()
    )


def _test_select_works(connection) -> None:
    """SELECT on a real table succeeds under the app role."""
    if not _role_exists(connection, _APP_ROLE):
        pytest.skip(f"Role {_APP_ROLE!r} does not exist on this server")

    # Insert a row as the owner so there's something to select.
    uid = uuid.uuid4()
    connection.execute(
        sa.text("INSERT INTO users (id, email, google_id) VALUES (:id, :e, :g)"),
        {"id": uid, "e": f"{uid}@example.com", "g": f"g-{uid}"},
    )

    connection.execute(sa.text(f"SET LOCAL ROLE {_APP_ROLE}"))
    count = connection.execute(
        sa.text("SELECT count(*) FROM users WHERE id = :id"), {"id": uid}
    ).scalar_one()
    connection.execute(sa.text("RESET ROLE"))

    assert count == 1, f"expected 1 row, got {count}"


def _test_ddl_is_denied(connection) -> None:
    """DROP TABLE and ALTER TABLE are denied for the app role."""
    if not _role_exists(connection, _APP_ROLE):
        pytest.skip(f"Role {_APP_ROLE!r} does not exist on this server")

    # DROP TABLE must fail.
    connection.execute(sa.text("SAVEPOINT before_drop"))
    connection.execute(sa.text(f"SET LOCAL ROLE {_APP_ROLE}"))
    with pytest.raises(ProgrammingError):
        connection.execute(sa.text("DROP TABLE users"))
    # The failed DDL aborts the transaction, so roll back to the savepoint
    # (which also undoes SET LOCAL ROLE) before running anything else.
    connection.execute(sa.text("ROLLBACK TO SAVEPOINT before_drop"))
    connection.execute(sa.text("RESET ROLE"))

    # ALTER TABLE ADD COLUMN must fail.
    connection.execute(sa.text("SAVEPOINT before_alter"))
    connection.execute(sa.text(f"SET LOCAL ROLE {_APP_ROLE}"))
    with pytest.raises(ProgrammingError):
        connection.execute(sa.text("ALTER TABLE users ADD COLUMN _probe_col int"))
    # The failed DDL aborts the transaction, so roll back to the savepoint
    # (which also undoes SET LOCAL ROLE) before running anything else.
    connection.execute(sa.text("ROLLBACK TO SAVEPOINT before_alter"))
    connection.execute(sa.text("RESET ROLE"))


async def test_select_works_under_app_role():
    """SELECT on users succeeds under bookshelf_app."""
    engine = create_async_engine(settings.async_database_url)
    try:
        async with engine.connect() as conn:
            if not await conn.run_sync(
                lambda c: inspect(c).has_table("alembic_version")
            ):
                pytest.skip("database not built by alembic upgrade head")
        async with engine.connect() as conn:
            trans = await conn.begin()
            try:
                await conn.run_sync(_test_select_works)
            finally:
                await trans.rollback()
    finally:
        await engine.dispose()


async def test_ddl_is_denied_for_app_role():
    """DROP TABLE and ALTER TABLE fail under bookshelf_app."""
    engine = create_async_engine(settings.async_database_url)
    try:
        async with engine.connect() as conn:
            if not await conn.run_sync(
                lambda c: inspect(c).has_table("alembic_version")
            ):
                pytest.skip("database not built by alembic upgrade head")
        async with engine.connect() as conn:
            trans = await conn.begin()
            try:
                await conn.run_sync(_test_ddl_is_denied)
            finally:
                await trans.rollback()
    finally:
        await engine.dispose()


async def test_query_timeout_raises_query_canceled_error():
    """A query exceeding statement_timeout raises QueryCanceledError."""
    try:
        import asyncpg.exceptions as _asyncpg_exc
    except ImportError:
        pytest.skip("asyncpg not installed")

    # Use a very short timeout (1 ms) on a dedicated connection so this test
    # doesn't affect the shared pool.
    engine = create_async_engine(settings.async_database_url, pool_size=1)
    try:
        async with engine.connect() as conn:
            if not await conn.run_sync(
                lambda c: inspect(c).has_table("alembic_version")
            ):
                pytest.skip("database not built by alembic upgrade head")
            # Set a 1 ms timeout, then run pg_sleep(1) which will exceed it.
            await conn.execute(sa.text("SET statement_timeout = 1"))
            with pytest.raises(Exception) as exc_info:
                await conn.execute(sa.text("SELECT pg_sleep(1)"))
            # asyncpg may wrap the exception; walk the cause chain.
            cause = exc_info.value
            found = False
            while cause is not None:
                if isinstance(cause, _asyncpg_exc.QueryCanceledError):
                    found = True
                    break
                cause = getattr(cause, "__cause__", None) or getattr(
                    cause, "__context__", None
                )
            assert found, (
                f"expected QueryCanceledError in the exception chain; "
                f"got: {exc_info.value!r}"
            )
    finally:
        await engine.dispose()

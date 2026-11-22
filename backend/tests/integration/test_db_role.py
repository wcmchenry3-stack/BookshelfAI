"""Integration tests for the bookshelf_app DB role (#696).

On a database built by ``alembic upgrade head`` (migration 0028 has run):

- the role exists, can log in, has ``BYPASSRLS`` and no superuser /
  ``CREATEROLE`` / ``CREATEDB``;
- DML works under the role;
- DDL is denied with SQLSTATE 42501 (insufficient_privilege): ``DROP TABLE``,
  ``ALTER TABLE`` and ``CREATE TABLE`` in schema ``public``;
- every table in ``public`` (except ``alembic_version``) has exactly
  ``SELECT, INSERT, UPDATE, DELETE`` for the role and every sequence has
  ``USAGE`` only, so a table or sequence added without grants fails the build;
- ``scripts/db/app_role_grants.sql`` (the restore runbook's grants,
  docs/backups.md) brings those grants back after they are lost.

A missing role or missing grants FAIL these tests; they never skip for it.
The only skip is for a database that was not built by alembic (no
``alembic_version``), like every other migration-dependent integration test.
Every change runs in a transaction that is rolled back.
"""

from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.core.database import APP_DB_ROLE

pytestmark = pytest.mark.integration

_INSUFFICIENT_PRIVILEGE = "42501"
_DML = ("SELECT", "INSERT", "UPDATE", "DELETE")
_NOT_GRANTED = ("TRUNCATE", "REFERENCES", "TRIGGER")
_GRANTS_SQL = Path(__file__).resolve().parents[3] / "scripts/db/app_role_grants.sql"

_APP_TABLES = sa.text("""
    SELECT c.relname FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
      AND c.relname <> 'alembic_version'
    ORDER BY c.relname
    """)
_SEQUENCES = sa.text("""
    SELECT c.relname FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind = 'S'
    ORDER BY c.relname
    """)


async def run_rolled_back(fn) -> None:
    """Run ``fn(sync_connection)`` in a transaction that is always rolled back,
    on a database built by alembic (skip otherwise)."""
    engine = create_async_engine(settings.async_database_url)
    try:
        async with engine.connect() as conn:
            if not await conn.run_sync(
                lambda c: inspect(c).has_table("alembic_version")
            ):
                pytest.skip("database not built by alembic upgrade head")
            await conn.rollback()  # end the inspector's autobegun transaction
            trans = await conn.begin()
            try:
                await conn.run_sync(fn)
            finally:
                await trans.rollback()
    finally:
        await engine.dispose()


def _scalar(connection, sql: str, **params):
    return connection.execute(sa.text(sql), params).scalar()


def _assert_denied(connection, statement: str) -> None:
    """``statement`` run as the app role fails with insufficient_privilege."""
    connection.execute(sa.text("SAVEPOINT probe"))
    connection.execute(sa.text(f"SET LOCAL ROLE {APP_DB_ROLE}"))
    with pytest.raises(ProgrammingError) as exc_info:
        connection.execute(sa.text(statement))
    # A failed statement aborts the transaction: roll back to the savepoint
    # (which also undoes SET LOCAL ROLE) before running anything else.
    connection.execute(sa.text("ROLLBACK TO SAVEPOINT probe"))
    connection.execute(sa.text("RESET ROLE"))
    sqlstate = getattr(exc_info.value.orig, "sqlstate", None)
    assert (
        sqlstate == _INSUFFICIENT_PRIVILEGE
    ), f"{statement!r} failed with {sqlstate}, not 42501"


def _table_privileges(connection, table: str) -> dict[str, bool]:
    return {
        p: bool(
            _scalar(
                connection,
                "SELECT has_table_privilege(:r, :t, :p)",
                r=APP_DB_ROLE,
                t=f"public.{table}",
                p=p,
            )
        )
        for p in _DML + _NOT_GRANTED
    }


def _sequence_problems(connection) -> list[str]:
    problems = []
    for sequence in connection.execute(_SEQUENCES).scalars().all():
        for privilege, expected in (
            ("USAGE", True),
            ("SELECT", False),
            ("UPDATE", False),
        ):
            held = _scalar(
                connection,
                "SELECT has_sequence_privilege(:r, :s, :p)",
                r=APP_DB_ROLE,
                s=f"public.{sequence}",
                p=privilege,
            )
            if bool(held) != expected:
                problems.append(f"sequence {sequence}: {privilege} is {held}")
    return problems


def grant_problems(connection) -> list[str]:
    """Every way the app role's grants differ from what 0028 sets up."""
    tables = connection.execute(_APP_TABLES).scalars().all()
    assert "users" in tables and "scan_records" in tables, tables
    problems = []
    for table in tables:
        privileges = _table_privileges(connection, table)
        missing = [p for p in _DML if not privileges[p]]
        extra = [p for p in _NOT_GRANTED if privileges[p]]
        if missing:
            problems.append(f"{table}: missing {', '.join(missing)}")
        if extra:
            problems.append(f"{table}: unexpected {', '.join(extra)}")
    problems += _sequence_problems(connection)
    alembic = _table_privileges(connection, "alembic_version")
    if any(alembic.values()):
        problems.append(f"alembic_version: unexpected {alembic}")
    return problems


def _assert_no_problems(problems: list[str]) -> None:
    assert problems == [], "\n".join(problems)


async def test_app_role_exists_with_only_the_expected_attributes():
    def check(connection):
        row = connection.execute(
            sa.text(
                "SELECT rolcanlogin, rolbypassrls, rolsuper, rolcreaterole, "
                "rolcreatedb FROM pg_roles WHERE rolname = :r"
            ),
            {"r": APP_DB_ROLE},
        ).one_or_none()
        assert row is not None, (
            f"role {APP_DB_ROLE!r} does not exist on a migrated database "
            "(migration 0028 requires it)"
        )
        assert tuple(row) == (True, True, False, False, False), row

    await run_rolled_back(check)


async def test_dml_works_under_app_role():
    """SELECT / INSERT / UPDATE / DELETE on users succeed as bookshelf_app."""

    def check(connection):
        uid = _scalar(connection, "SELECT gen_random_uuid()")
        count = "SELECT count(*) FROM users WHERE id = :id"
        connection.execute(sa.text(f"SET LOCAL ROLE {APP_DB_ROLE}"))
        connection.execute(
            sa.text("INSERT INTO users (id, email, google_id) VALUES (:id, :e, :g)"),
            {"id": uid, "e": f"{uid}@example.com", "g": f"g-{uid}"},
        )
        connection.execute(
            sa.text("UPDATE users SET display_name = 'x' WHERE id = :id"), {"id": uid}
        )
        assert _scalar(connection, count, id=uid) == 1
        connection.execute(sa.text("DELETE FROM users WHERE id = :id"), {"id": uid})
        assert _scalar(connection, count, id=uid) == 0
        connection.execute(sa.text("RESET ROLE"))

    await run_rolled_back(check)


async def test_ddl_is_denied_for_app_role():
    """DROP / ALTER on an app table and CREATE in schema public all fail with
    42501 (security review S1: CREATE is checked explicitly, since Supabase
    sets its own grants on schema public)."""

    def check(connection):
        _assert_denied(connection, "DROP TABLE users")
        _assert_denied(connection, "ALTER TABLE users ADD COLUMN _probe_col int")
        _assert_denied(connection, "CREATE TABLE public._probe_create (id int)")
        has_create = _scalar(
            connection,
            "SELECT has_schema_privilege(:r, 'public', 'CREATE')",
            r=APP_DB_ROLE,
        )
        assert has_create is False

    await run_rolled_back(check)


async def test_every_app_table_and_sequence_is_granted():
    """Every table in public (except alembic_version) has exactly DML for the
    app role and every sequence has USAGE only. A table or sequence added
    without grants (e.g. created by a role whose default privileges do not
    cover it) fails here instead of 500ing once DATABASE_URL is the app role."""
    await run_rolled_back(lambda c: _assert_no_problems(grant_problems(c)))


async def test_grant_check_catches_a_missing_grant():
    """The check above is not vacuous: revoking one table grant and one
    sequence grant makes it report both."""

    def check(connection):
        sequence = connection.execute(_SEQUENCES).scalars().first()
        assert sequence is not None
        connection.execute(sa.text(f"REVOKE DELETE ON scan_records FROM {APP_DB_ROLE}"))
        connection.execute(
            sa.text(f"REVOKE USAGE ON SEQUENCE public.{sequence} FROM {APP_DB_ROLE}")
        )
        problems = grant_problems(connection)
        assert "scan_records: missing DELETE" in problems
        assert f"sequence {sequence}: USAGE is False" in problems

    await run_rolled_back(check)


async def test_restore_grants_script_reapplies_every_grant():
    """docs/backups.md's restore step: after a --no-acl restore the role has
    no grants; scripts/db/app_role_grants.sql (run as the owner) brings back
    exactly what 0028 set up, and running it twice is harmless."""

    def check(connection):
        for kind in ("TABLES", "SEQUENCES"):
            connection.execute(
                sa.text(f"REVOKE ALL ON ALL {kind} IN SCHEMA public FROM {APP_DB_ROLE}")
            )
        assert grant_problems(connection) != []
        script = _GRANTS_SQL.read_text()
        connection.exec_driver_sql(script)
        connection.exec_driver_sql(script)
        _assert_no_problems(grant_problems(connection))

    await run_rolled_back(check)

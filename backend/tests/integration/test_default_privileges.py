"""Integration tests for bookshelf_app default privileges (#696).

Verifies that a table created *after* migration 0028 automatically inherits
SELECT / INSERT / UPDATE / DELETE for ``bookshelf_app`` via the default
privileges set by the migration.

Everything runs in a rolled-back transaction so no persistent changes are made.
The test skips when the role does not exist (plain Postgres / CI without the
role) or when the database has not been built by alembic upgrade head.
"""

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings

pytestmark = pytest.mark.integration

_APP_ROLE = "bookshelf_app"
_PROBE_TABLE = "default_privileges_probe_0028"


def _has_privilege(connection, role: str, table: str, privilege: str) -> bool:
    return bool(
        connection.execute(
            sa.text("SELECT has_table_privilege(:r, :t, :p)"),
            {"r": role, "t": f"public.{table}", "p": privilege},
        ).scalar_one()
    )


def _test_new_table_gets_grants(connection) -> None:
    """A freshly-created table inherits DML grants for bookshelf_app."""
    role_exists = bool(
        connection.execute(
            sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"),
            {"r": _APP_ROLE},
        ).scalar()
    )
    if not role_exists:
        pytest.skip(f"Role {_APP_ROLE!r} does not exist on this server")

    # Check that default privileges are set (migration 0028 must have run).
    default_acl_set = bool(
        connection.execute(
            sa.text(
                "SELECT 1 FROM pg_default_acl d, aclexplode(d.defaclacl) a "
                "WHERE d.defaclrole = current_user::regrole "
                "AND d.defaclnamespace = 'public'::regnamespace "
                "AND d.defaclobjtype = 'r' "
                "AND a.grantee::regrole::text = :role "
                "AND a.privilege_type = 'SELECT'"
            ),
            {"role": _APP_ROLE},
        ).scalar()
    )
    if not default_acl_set:
        pytest.skip(
            "Default privileges for bookshelf_app not found — "
            "migration 0028 may not have run on this database"
        )

    # Create a new table (inside the transaction, will be rolled back).
    connection.execute(
        sa.text(f"CREATE TABLE public.{_PROBE_TABLE} (id int PRIMARY KEY)")
    )

    # Verify that SELECT, INSERT, UPDATE, DELETE are all granted automatically.
    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        granted = _has_privilege(connection, _APP_ROLE, _PROBE_TABLE, privilege)
        assert granted, (
            f"Expected {privilege} on {_PROBE_TABLE} to be granted to "
            f"{_APP_ROLE} via default privileges, but it was not"
        )

    # Verify that DDL-adjacent privileges are NOT granted.
    for privilege in ("TRUNCATE", "REFERENCES", "TRIGGER"):
        granted = _has_privilege(connection, _APP_ROLE, _PROBE_TABLE, privilege)
        assert not granted, (
            f"Expected {privilege} on {_PROBE_TABLE} to NOT be granted to "
            f"{_APP_ROLE}, but it was"
        )


async def test_new_table_inherits_dml_grants():
    """A table created after 0028 automatically gets DML grants for bookshelf_app."""
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
                await conn.run_sync(_test_new_table_gets_grants)
            finally:
                await trans.rollback()
    finally:
        await engine.dispose()

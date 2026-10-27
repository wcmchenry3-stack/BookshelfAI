"""Supabase grants (migration 0022): anon/authenticated lose table privileges.

Supabase grants its Data API roles ``anon`` and ``authenticated`` ``ALL`` on
every table and sequence in ``public``, plus default privileges for future
ones. 0022 revokes them when the roles exist (and checks that nothing is left),
and does nothing otherwise. Its downgrade is a no-op.

Plain Postgres (CI) has neither role, so the cycle test creates stand-ins
inside a transaction, grants them what Supabase grants, runs the migration and
rolls everything back. Roles are cluster-wide, but ``CREATE ROLE`` is
transactional, so the rollback leaves none behind. If a role already exists
on the server it is reused (its grants are changed only inside the rolled-back
transaction) and never dropped.
"""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings

pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0022_revoke_supabase_grants.py"
)
ROLES = ("anon", "authenticated")
TABLES = ("users", "refresh_tokens", "user_books", "books")
TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
# A BIGSERIAL primary key, so it owns a sequence.
SEQUENCE_OF = ("ai_usage", "id")
PROBE_TABLE = "supabase_grants_probe_0022"


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0022", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _role_exists(connection, role: str) -> bool:
    return bool(
        connection.execute(
            sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
        ).scalar()
    )


def _sequence(connection) -> str:
    name = connection.execute(
        sa.text("SELECT pg_get_serial_sequence(:t, :c)"),
        {"t": SEQUENCE_OF[0], "c": SEQUENCE_OF[1]},
    ).scalar_one()
    assert name, f"{SEQUENCE_OF} has no sequence"
    return name


def _table_privs(connection, role: str, table: str) -> dict[str, bool]:
    return {
        p: connection.execute(
            sa.text("SELECT has_table_privilege(:r, :t, :p)"),
            {"r": role, "t": f"public.{table}", "p": p},
        ).scalar_one()
        for p in TABLE_PRIVILEGES
    }


def _sequence_privs(connection, role: str, sequence: str) -> dict[str, bool]:
    return {
        p: connection.execute(
            sa.text("SELECT has_sequence_privilege(:r, :s, :p)"),
            {"r": role, "s": sequence, "p": p},
        ).scalar_one()
        for p in ("USAGE", "SELECT", "UPDATE")
    }


def _new_table_select(connection, role: str, suffix: str) -> bool:
    """Whether ``role`` can SELECT a table created now (default privileges)."""
    table = f"{PROBE_TABLE}_{suffix}"
    connection.execute(sa.text(f"CREATE TABLE public.{table} (id int)"))
    return connection.execute(
        sa.text("SELECT has_table_privilege(:r, :t, 'SELECT')"),
        {"r": role, "t": f"public.{table}"},
    ).scalar_one()


def _acl_snapshot(connection):
    """Every ACL the migration could touch: public relations and defaults."""
    rels = connection.execute(
        sa.text(
            "SELECT c.relname, c.relacl::text FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'S') "
            "ORDER BY c.relname"
        )
    ).all()
    defaults = connection.execute(
        sa.text(
            "SELECT defaclrole::regrole::text, defaclobjtype, defaclacl::text "
            "FROM pg_default_acl ORDER BY 1, 2"
        )
    ).all()
    return rels, defaults


def _assert_nothing_granted(connection) -> None:
    """No ACL entry for either role on any public table, sequence or default."""
    rows = connection.execute(
        sa.text(
            "SELECT c.relname, a.grantee::regrole::text, a.privilege_type "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace, "
            "aclexplode(c.relacl) a "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'S') "
            "AND a.grantee::regrole::text IN ('anon', 'authenticated')"
        )
    ).all()
    assert rows == [], rows
    defaults = connection.execute(
        sa.text(
            "SELECT d.defaclobjtype, a.grantee::regrole::text, a.privilege_type "
            "FROM pg_default_acl d, aclexplode(d.defaclacl) a "
            "WHERE d.defaclrole = current_user::regrole "
            "AND d.defaclnamespace = 'public'::regnamespace "
            "AND a.grantee::regrole::text IN ('anon', 'authenticated')"
        )
    ).all()
    assert defaults == [], defaults


def _with_migration(connection, fn) -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration = _load_migration()
    assert (migration.revision, migration.down_revision) == ("0022", "0021")
    assert tuple(migration.ROLES) == ROLES
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        fn(migration)


def _migration_cycle(connection) -> None:
    for role in ROLES:
        if not _role_exists(connection, role):
            connection.execute(sa.text(f"CREATE ROLE {role} NOLOGIN NOBYPASSRLS"))
        # What Supabase grants, including defaults for the migrating role.
        connection.execute(sa.text(f"GRANT USAGE ON SCHEMA public TO {role}"))
        connection.execute(
            sa.text(f"GRANT ALL ON ALL TABLES IN SCHEMA public TO {role}")
        )
        connection.execute(
            sa.text(f"GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO {role}")
        )
        connection.execute(
            sa.text(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO {role}"
            )
        )
        connection.execute(
            sa.text(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                f"GRANT ALL ON SEQUENCES TO {role}"
            )
        )
    sequence = _sequence(connection)

    # The stand-in matches Supabase's starting point.
    for role in ROLES:
        for table in TABLES:
            assert all(_table_privs(connection, role, table).values()), (role, table)
        assert all(_sequence_privs(connection, role, sequence).values()), role
    assert _new_table_select(connection, "anon", "before")

    def cycle(migration):
        migration.upgrade()
        for role in ROLES:
            for table in TABLES:
                privs = _table_privs(connection, role, table)
                assert not any(privs.values()), (role, table, privs)
            privs = _sequence_privs(connection, role, sequence)
            assert not any(privs.values()), (role, privs)
            # Default privileges: a table created afterwards isn't granted.
            assert not _new_table_select(connection, role, f"up_{role}")
        _assert_nothing_granted(connection)
        # The owner (the app's role) keeps everything.
        me = connection.execute(sa.text("SELECT current_user")).scalar_one()
        assert all(_table_privs(connection, me, "users").values())

        # Downgrade is a no-op: grants are not restored.
        migration.downgrade()
        _assert_nothing_granted(connection)
        for role in ROLES:
            for table in TABLES:
                privs = _table_privs(connection, role, table)
                assert not any(privs.values()), (role, table, privs)
            assert not _new_table_select(connection, role, f"down_{role}")

    _with_migration(connection, cycle)


def _migration_noop(connection) -> None:
    if any(_role_exists(connection, r) for r in ROLES):
        pytest.skip("anon/authenticated exist on this server")
    before = _acl_snapshot(connection)

    def cycle(migration):
        migration.upgrade()
        assert _acl_snapshot(connection) == before
        migration.downgrade()
        assert _acl_snapshot(connection) == before

    _with_migration(connection, cycle)


def _migration_foreign_grantor(connection) -> None:
    """A grant the owner's REVOKE can't remove makes the upgrade raise."""
    helper = "grants_helper_0022"
    for role in ROLES:
        if not _role_exists(connection, role):
            connection.execute(sa.text(f"CREATE ROLE {role} NOLOGIN NOBYPASSRLS"))
    connection.execute(sa.text(f"CREATE ROLE {helper} NOLOGIN"))
    connection.execute(
        sa.text(f"GRANT ALL ON public.users TO {helper} WITH GRANT OPTION")
    )
    # Granted by the helper, so REVOKE by the owner leaves it in place.
    connection.execute(sa.text(f"SET LOCAL ROLE {helper}"))
    connection.execute(sa.text("GRANT TRUNCATE ON public.users TO anon"))
    connection.execute(sa.text("RESET ROLE"))
    assert _table_privs(connection, "anon", "users")["TRUNCATE"]

    def run(migration):
        connection.execute(sa.text("SAVEPOINT before_upgrade"))
        with pytest.raises(Exception, match="still holds privileges"):
            migration.upgrade()
        connection.execute(sa.text("ROLLBACK TO SAVEPOINT before_upgrade"))

    _with_migration(connection, run)


async def _run_rolled_back(fn) -> None:
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
                await conn.run_sync(fn)
            finally:
                await trans.rollback()
    finally:
        await engine.dispose()


async def test_migration_0022_revokes_supabase_grants_and_downgrade_is_a_noop():
    await _run_rolled_back(_migration_cycle)


async def test_migration_0022_is_a_noop_without_supabase_roles():
    await _run_rolled_back(_migration_noop)


async def test_migration_0022_raises_when_a_foreign_grant_survives_the_revoke():
    await _run_rolled_back(_migration_foreign_grantor)

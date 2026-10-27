"""Row level security (migration 0020): on for every table, no policies.

Two checks:

* every table in the ``public`` schema has RLS on. This needs a database built
  by ``alembic upgrade head`` (CI's test-migrations job does that before the
  integration suite), so it skips on a database the models built. A new table
  whose migration forgets ``ENABLE ROW LEVEL SECURITY`` fails here;
* the migration's cycle. With RLS on, a role that is neither the owner nor
  ``BYPASSRLS`` (standing in for Supabase's ``anon``) sees no rows and can't
  insert any, while the owner still can. Downgrade turns it off again.

Everything that changes the database runs in a transaction that is rolled
back, so the database is left as it was.
"""

import importlib.util
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.core.database import Base

pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0020_enable_rls.py"
)
PROBE_ROLE = "rls_probe_0020"


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0020", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rls_flags(connection) -> dict[str, bool]:
    rows = connection.execute(
        sa.text(
            "SELECT c.relname, c.relrowsecurity FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')"
        )
    )
    return {name: flag for name, flag in rows}


async def test_every_public_table_has_rls():
    engine = create_async_engine(settings.async_database_url)
    try:
        async with engine.connect() as conn:
            has_alembic = await conn.run_sync(
                lambda c: inspect(c).has_table("alembic_version")
            )
            if not has_alembic:
                pytest.skip("database not built by alembic upgrade head")
            flags = await conn.run_sync(_rls_flags)
            policies = (
                await conn.execute(
                    sa.text(
                        "SELECT tablename, policyname FROM pg_policies "
                        "WHERE schemaname = 'public'"
                    )
                )
            ).all()
    finally:
        await engine.dispose()

    assert flags, "no tables in the public schema"
    missing = sorted(name for name, on in flags.items() if not on)
    assert missing == [], f"tables without row level security: {missing}"
    # The guarantee is "RLS on, no policies": a policy (e.g. one granting
    # anon access) has to be added on purpose, together with this test.
    assert policies == [], f"unexpected RLS policies: {policies}"


def _migration_cycle(connection) -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration = _load_migration()
    assert (migration.revision, migration.down_revision) == ("0020", "0019")
    # 0020's list is frozen at revision 0019, so it only has to name tables the
    # models still define; tables added later enable RLS in their own migration
    # (checked by test_every_public_table_has_rls). If a later migration drops
    # or renames one of these tables, prune it from this test's view, never
    # from the migration.
    assert set(migration.TABLES) <= set(Base.metadata.tables) | {"alembic_version"}

    def probe_count() -> int:
        connection.execute(sa.text(f"SET LOCAL ROLE {PROBE_ROLE}"))
        count = connection.execute(sa.text("SELECT count(*) FROM users")).scalar_one()
        connection.execute(sa.text("RESET ROLE"))
        return count

    uid = uuid.uuid4()
    connection.execute(
        sa.text("INSERT INTO users (id, email, google_id) VALUES (:id, :e, :g)"),
        {"id": uid, "e": f"{uid}@example.com", "g": f"g-{uid}"},
    )
    connection.execute(sa.text(f"CREATE ROLE {PROBE_ROLE} NOLOGIN NOBYPASSRLS"))
    connection.execute(sa.text(f"GRANT USAGE ON SCHEMA public TO {PROBE_ROLE}"))
    connection.execute(sa.text(f"GRANT SELECT, INSERT ON users TO {PROBE_ROLE}"))

    context = MigrationContext.configure(connection)
    with Operations.context(context):
        migration.downgrade()
        assert not any(_rls_flags(connection)[t] for t in ("users", "books"))
        # RLS off: the grant alone lets the probe read every row.
        assert probe_count() >= 1

        migration.upgrade()
        flags = _rls_flags(connection)
        assert all(flags[t] for t in migration.TABLES)
        # RLS on, no policies: the probe sees nothing...
        assert probe_count() == 0
        # ...and can't write, while the owner still reads its row.
        nested = connection.begin_nested()
        connection.execute(sa.text(f"SET LOCAL ROLE {PROBE_ROLE}"))
        with pytest.raises(ProgrammingError, match="row-level security"):
            connection.execute(
                sa.text(
                    "INSERT INTO users (id, email, google_id) VALUES (:id, :e, :g)"
                ),
                {"id": uuid.uuid4(), "e": "probe@example.com", "g": "g-probe"},
            )
        nested.rollback()
        connection.execute(sa.text("RESET ROLE"))
        assert (
            connection.execute(
                sa.text("SELECT count(*) FROM users WHERE id = :id"), {"id": uid}
            ).scalar_one()
            == 1
        )


async def test_migration_0020_upgrade_downgrade_cycle():
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
                await conn.run_sync(_migration_cycle)
            finally:
                await trans.rollback()
    finally:
        await engine.dispose()

"""The library audit log purge function on real Postgres (#1042, migration 0035).

On a database built by ``alembic upgrade head``:

- ``bookshelf_app`` has no ``DELETE`` on ``library_events``: a direct
  ``DELETE`` fails with 42501.
- As ``bookshelf_app``, ``purge_library_events(cutoff, batch)`` deletes only
  rows older than the cutoff, at most ``batch`` a call, oldest first.
- A cutoff younger than 30 days (``now()``, the future) is clamped to the
  30-day floor; a NULL cutoff or a batch outside 1..10,000 is refused (22023).
- A caller's ``search_path`` cannot redirect it: with a temporary
  ``library_events`` first on the path, the function still deletes from
  ``public.library_events`` and leaves the temporary table alone.
- It is ``SECURITY DEFINER`` with a fixed ``search_path`` and its own
  ``lock_timeout``, and only the app role may execute it (not ``PUBLIC``,
  ``anon`` or ``authenticated``).
- The retention job stops on the caller's ``statement_timeout`` (the job
  session's) and the function's ``lock_timeout`` while a lock blocks it.
- Migration 0035 downgrades (``DELETE`` back, function gone), re-runs and
  upgrades again cleanly.

Every change runs in a transaction that is rolled back, except the two
timeout tests, which only hold a lock and roll back too. Skips only on a
database not built by alembic.
"""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.core.database import APP_DB_ROLE, JobSessionLocal, engine
from app.services import library_events_retention as retention
from tests.integration.test_db_role import _assert_denied, run_rolled_back

pytestmark = pytest.mark.integration

FUNCTION = "public.purge_library_events(timestamptz, integer)"
MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0035_library_events_purge_function.py"
)
_INVALID_PARAMETER = "22023"
_QUERY_CANCELED = "57014"
_LOCK_NOT_AVAILABLE = "55P03"


def _scalar(connection, sql: str, **params):
    return connection.execute(sa.text(sql), params).scalar()


def _seed(connection, *ages_in_days: int) -> None:
    """Start from a table with nothing older than 30 days (rolled back with
    the test), then add one event per age."""
    connection.execute(
        sa.text(
            "DELETE FROM library_events WHERE created_at < now() - interval '30 days'"
        )
    )
    for age in ages_in_days:
        connection.execute(
            sa.text(
                "INSERT INTO library_events (event_type, created_at) "
                "VALUES ('updated', now() - make_interval(days => :d))"
            ),
            {"d": age},
        )


def _ages(connection) -> list[int]:
    """Ages in days of the events older than 25 days in public.library_events
    (qualified: a temporary table of that name would shadow it), oldest first."""
    return list(
        connection.execute(
            sa.text(
                "SELECT extract(day FROM now() - created_at)::int "
                "FROM public.library_events "
                "WHERE created_at < now() - interval '25 days' "
                "ORDER BY created_at"
            )
        ).scalars()
    )


def _purge_as_app(connection, cutoff_sql: str, batch: int) -> int:
    connection.execute(sa.text(f"SET LOCAL ROLE {APP_DB_ROLE}"))
    deleted = _scalar(
        connection,
        f"SELECT purge_library_events({cutoff_sql}, :b)",
        b=batch,
    )
    connection.execute(sa.text("RESET ROLE"))
    return deleted


def _refused_as_app(connection, statement: str) -> str | None:
    connection.execute(sa.text("SAVEPOINT probe"))
    connection.execute(sa.text(f"SET LOCAL ROLE {APP_DB_ROLE}"))
    with pytest.raises(DBAPIError) as exc_info:
        connection.execute(sa.text(statement))
    connection.execute(sa.text("ROLLBACK TO SAVEPOINT probe"))
    connection.execute(sa.text("RESET ROLE"))
    return getattr(exc_info.value.orig, "sqlstate", None)


async def test_app_role_cannot_delete_events():
    def check(connection):
        _seed(connection, 400)
        _assert_denied(connection, "DELETE FROM library_events")
        assert _ages(connection) == [400]

    await run_rolled_back(check)


async def test_function_deletes_only_older_rows_in_batches():
    def check(connection):
        _seed(connection, 400, 401, 402, 403, 404, 200, 201, 10)
        cutoff = "now() - interval '300 days'"
        # Oldest first, at most two a call.
        assert _purge_as_app(connection, cutoff, 2) == 2
        assert _ages(connection) == [402, 401, 400, 201, 200]
        assert _purge_as_app(connection, cutoff, 2) == 2
        assert _purge_as_app(connection, cutoff, 2) == 1
        assert _purge_as_app(connection, cutoff, 2) == 0
        assert _ages(connection) == [201, 200]
        assert _purge_as_app(connection, "now() - interval '100 days'", 10) == 2
        assert _ages(connection) == []
        # The 10-day-old event is never touched.
        assert _scalar(connection, "SELECT count(*) FROM library_events") >= 1

    await run_rolled_back(check)


@pytest.mark.parametrize(
    "cutoff_sql",
    ["now()", "now() + interval '1 year'", "now() - interval '29 days'"],
)
async def test_a_too_recent_cutoff_is_clamped_to_30_days(cutoff_sql):
    def check(connection):
        _seed(connection, 31, 29)
        assert _purge_as_app(connection, cutoff_sql, 100) == 1
        assert _ages(connection) == [29]

    await run_rolled_back(check)


@pytest.mark.parametrize(
    "call",
    [
        "SELECT purge_library_events(NULL, 10)",
        "SELECT purge_library_events(now() - interval '400 days', NULL)",
        "SELECT purge_library_events(now() - interval '400 days', 0)",
        "SELECT purge_library_events(now() - interval '400 days', -1)",
        "SELECT purge_library_events(now() - interval '400 days', 10001)",
    ],
)
async def test_invalid_arguments_are_refused(call):
    def check(connection):
        _seed(connection, 400)
        assert _refused_as_app(connection, call) == _INVALID_PARAMETER
        assert _ages(connection) == [400]

    await run_rolled_back(check)


async def test_search_path_cannot_redirect_the_delete():
    """The app role shadows the table with a temporary one and puts pg_temp
    first: the function still deletes from public.library_events only."""

    def check(connection):
        _seed(connection, 400)
        connection.execute(sa.text(f"SET LOCAL ROLE {APP_DB_ROLE}"))
        connection.execute(
            sa.text(
                "CREATE TEMP TABLE library_events (id bigint, created_at "
                "timestamptz) ON COMMIT DROP"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO pg_temp.library_events "
                "VALUES (1, now() - interval '400 days')"
            )
        )
        connection.execute(sa.text("SET LOCAL search_path = pg_temp, public"))
        deleted = _scalar(
            connection,
            "SELECT public.purge_library_events(now() - interval '300 days', 10)",
        )
        temp_rows = _scalar(connection, "SELECT count(*) FROM pg_temp.library_events")
        connection.execute(sa.text("RESET search_path"))
        connection.execute(sa.text("RESET ROLE"))
        assert (deleted, temp_rows) == (1, 1)
        assert _ages(connection) == []

    await run_rolled_back(check)


async def test_function_is_security_definer_with_fixed_settings_and_acl():
    def check(connection):
        row = connection.execute(
            sa.text(
                "SELECT p.prosecdef, p.proconfig, "
                "pg_get_userbyid(p.proowner) = t.tableowner "
                "FROM pg_proc p, pg_tables t WHERE p.oid = to_regprocedure(:f) "
                "AND t.schemaname = 'public' AND t.tablename = 'library_events'"
            ),
            {"f": FUNCTION},
        ).one()
        security_definer, config, owned_by_table_owner = row
        assert security_definer is True
        assert "search_path=public, pg_temp" in config
        assert "lock_timeout=5s" in config
        assert owned_by_table_owner is True
        # Nobody but the app role (and the owner) may call it.
        public_grants = _scalar(
            connection,
            "SELECT count(*) FROM pg_proc p, aclexplode(coalesce(p.proacl, "
            "acldefault('f', p.proowner))) a "
            "WHERE p.oid = to_regprocedure(:f) AND a.grantee = 0",
            f=FUNCTION,
        )
        assert public_grants == 0
        assert _scalar(
            connection,
            "SELECT has_function_privilege(:r, :f, 'EXECUTE')",
            r=APP_DB_ROLE,
            f=FUNCTION,
        )
        for role in ("anon", "authenticated"):
            if _scalar(connection, "SELECT 1 FROM pg_roles WHERE rolname = :r", r=role):
                assert not _scalar(
                    connection,
                    "SELECT has_function_privilege(:r, :f, 'EXECUTE')",
                    r=role,
                    f=FUNCTION,
                ), role

    await run_rolled_back(check)


# ── Timeouts: a lock held by another connection blocks the delete ────────────


async def _hold_table_lock(holder) -> None:
    await holder.execute(
        sa.text("LOCK TABLE public.library_events IN ACCESS EXCLUSIVE MODE")
    )


async def test_function_gives_up_on_its_lock_timeout():
    probe = create_async_engine(settings.async_database_url)
    try:
        async with probe.connect() as conn:
            if not await conn.run_sync(
                lambda c: inspect(c).has_table("alembic_version")
            ):
                pytest.skip("database not built by alembic upgrade head")
        async with probe.connect() as holder, probe.connect() as caller:
            await _hold_table_lock(holder)
            started = datetime.now(UTC)
            with pytest.raises(DBAPIError) as exc_info:
                await caller.execute(
                    sa.text(
                        "SELECT purge_library_events("
                        "now() - interval '400 days', 10)"
                    )
                )
            waited = (datetime.now(UTC) - started).total_seconds()
            await caller.rollback()
            await holder.rollback()
        assert getattr(exc_info.value.orig, "sqlstate", None) == _LOCK_NOT_AVAILABLE
        assert 4 <= waited < 30
    finally:
        await probe.dispose()


async def test_retention_job_runs_under_the_job_statement_timeout(monkeypatch):
    """Through JobSessionLocal, as token_cleanup runs it: a blocked purge is
    cancelled by the job session's statement_timeout."""
    probe = create_async_engine(settings.async_database_url)
    await engine.dispose()
    try:
        async with probe.connect() as conn:
            if not await conn.run_sync(
                lambda c: inspect(c).has_table("alembic_version")
            ):
                pytest.skip("database not built by alembic upgrade head")
        monkeypatch.setattr(settings, "statement_timeout_job_ms", 300)
        async with probe.connect() as holder:
            await _hold_table_lock(holder)
            try:
                with pytest.raises(DBAPIError) as exc_info:
                    async with JobSessionLocal() as db:
                        await retention.purge_old_library_events(db)
            finally:
                await holder.rollback()
        assert getattr(exc_info.value.orig, "sqlstate", None) == _QUERY_CANCELED
    finally:
        await probe.dispose()
        await engine.dispose()


# ── Migration 0035 ───────────────────────────────────────────────────────────


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0035", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _app_can_delete(connection) -> bool:
    return bool(
        _scalar(
            connection,
            "SELECT has_table_privilege(:r, 'public.library_events', 'DELETE')",
            r=APP_DB_ROLE,
        )
    )


def _has_function(connection) -> bool:
    return _scalar(connection, "SELECT to_regprocedure(:f) IS NOT NULL", f=FUNCTION)


def _ensure_role(connection, role: str) -> None:
    """A stand-in for a Supabase role (rolled back with the test)."""
    if not _scalar(connection, "SELECT 1 FROM pg_roles WHERE rolname = :r", r=role):
        connection.execute(sa.text(f"CREATE ROLE {role} NOLOGIN"))


def _migration_cycle(connection) -> None:
    migration = _load_migration()
    assert (migration.revision, migration.down_revision) == ("0035", "0034")
    assert retention.PURGE_BATCH_SIZE <= migration._MAX_BATCH
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        migration.downgrade()
        assert not _has_function(connection)
        assert _app_can_delete(connection)
        migration.downgrade()  # re-run after a partial downgrade
        assert not _has_function(connection)

        # What Supabase's default privileges would grant on a new function.
        for role in ("anon", "authenticated"):
            _ensure_role(connection, role)
        migration.upgrade()
        assert _has_function(connection)
        assert not _app_can_delete(connection)

        # Resume / re-run: a second upgrade over a finished one, with grants
        # drifted back, ends in the same state.
        for role in ("anon", "authenticated", "PUBLIC"):
            connection.execute(
                sa.text(f"GRANT EXECUTE ON FUNCTION {FUNCTION} TO {role}")
            )
        connection.execute(sa.text(f"GRANT DELETE ON library_events TO {APP_DB_ROLE}"))
        migration.upgrade()
    assert not _app_can_delete(connection)
    for role in ("anon", "authenticated"):
        assert not _scalar(
            connection,
            "SELECT has_function_privilege(:r, :f, 'EXECUTE')",
            r=role,
            f=FUNCTION,
        ), role
    assert _scalar(
        connection,
        "SELECT has_function_privilege(:r, :f, 'EXECUTE')",
        r=APP_DB_ROLE,
        f=FUNCTION,
    )
    _seed(connection, 400, 10)
    assert _purge_as_app(connection, "now()", 10) == 1


async def test_migration_0035_downgrade_rerun_and_upgrade():
    await run_rolled_back(_migration_cycle)

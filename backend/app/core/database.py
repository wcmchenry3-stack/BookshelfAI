import asyncio
import logging

from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session

from app.core.config import settings

# SQLite (local/CI) doesn't support pool sizing knobs; Postgres (Render dev,
# Supabase session pooler in prod) needs a bounded pool with pre-ping so a
# recycled/dropped connection is detected before it's handed to a request
# rather than surfacing as a mid-request error.
#
# db_pool_size / db_max_overflow are deliberately small: Render zero-downtime
# deploys run 2 instances of this service concurrently, plus a one-off
# `alembic upgrade head` connection during the deploy —
# 2 * (db_pool_size + db_max_overflow) + 1 must stay under the ~15-client cap
# of a small Supabase session pooler. With the defaults (3, 2) that's
# 2 * (3 + 2) + 1 = 11.
#
# The one "is this SQLite?" check for this module: the engine is built once,
# from this URL, so everything below that depends on the dialect reads it.
_is_sqlite = settings.async_database_url.startswith("sqlite")

logger = logging.getLogger(__name__)

# The least-privilege login role the app should run as (#696, migration 0028).
APP_DB_ROLE = "bookshelf_app"

_engine_kwargs = (
    {}
    if _is_sqlite
    else {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_pre_ping": True,
        "pool_recycle": 1800,
    }
)


def _statement_timeout_connect_args() -> dict:
    """asyncpg connect args that set statement_timeout at connection startup (#696).

    asyncpg takes Postgres startup parameters as ``server_settings``. A libpq
    style ``?options=-c statement_timeout=...`` on DATABASE_URL does NOT work
    here: SQLAlchemy passes URL query keys to ``asyncpg.connect()``, which has
    no ``options`` argument, so every connect fails with TypeError. Applies to
    the app engine only; Alembic builds its own engine (alembic/env.py) and
    is never subject to this timeout.
    """
    ms = settings.statement_timeout_ms
    if _is_sqlite or ms <= 0:
        return {}
    return {"server_settings": {"statement_timeout": str(ms)}}


# hide_parameters: a DB error's message must not carry the bound values
# (google_id, names, emails) into logs or Sentry (#688 review).
engine = create_async_engine(
    settings.async_database_url,
    echo=False,
    hide_parameters=True,
    connect_args=_statement_timeout_connect_args(),
    **_engine_kwargs,
)


def _set_statement_timeout(dbapi_conn, _connection_record, _connection_proxy) -> None:
    """Re-apply statement_timeout on every pool checkout (#696).

    The startup parameter above is the first layer. This SET is the second,
    because the startup parameter alone is not guaranteed in prod: the app
    reaches Supabase through its Supavisor *session* pooler (port 5432, see
    config.py), and a pooler may not forward client startup parameters to the
    server connection it hands us. In session mode the server connection stays
    ours for the life of the client connection and is not reset under us, so
    one SET per connection would usually do; it runs per checkout instead
    because it executes inside the adapter's implicit transaction, and a
    rollback at check-in (the pool's reset-on-return) undoes a SET made in a
    transaction. One cheap round trip per checkout keeps the timeout in force
    whatever the pooler or the previous checkout did. Skipped for SQLite and
    when the timeout is disabled (0 or negative).

    Why a raw ``SET`` (CLAUDE.md rule 7 is "SQLAlchemy ORM only"): ``SET`` is
    session configuration, not a query on a table, and neither the ORM nor
    Core has a construct for it. This runs on the raw DBAPI connection inside
    a pool event, below the ORM. The only interpolated value is
    ``statement_timeout_ms``, a pydantic-validated ``int``.
    """
    if _is_sqlite:
        return
    ms = settings.statement_timeout_ms
    if ms <= 0:
        return
    cursor = dbapi_conn.cursor()
    cursor.execute(f"SET statement_timeout = {ms}")
    cursor.close()


# Register on the sync engine that underlies the async wrapper. The "checkout"
# event fires whenever asyncpg hands a connection from the pool to a caller.
event.listen(engine.sync_engine, "checkout", _set_statement_timeout)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class _JobSession(Session):
    """Sync session class behind :data:`JobSessionLocal` (see below)."""


def _set_job_statement_timeout(_session, _transaction, connection) -> None:
    """``SET LOCAL statement_timeout`` to the background-job value at the start
    of every transaction of a :data:`JobSessionLocal` session (#696).

    ``SET LOCAL`` lasts until the transaction ends, so the longer job timeout
    never leaks onto the pooled connection: the next request that checks it
    out gets the app value back from :func:`_set_statement_timeout`. It runs
    per transaction (``after_begin``), not once per session, because the purge
    jobs commit every batch. Raw ``SET`` for the reason given in
    :func:`_set_statement_timeout`; the value is a validated ``int``. 0
    disables the timeout for jobs. Skipped on SQLite.
    """
    if _is_sqlite:
        return
    ms = max(settings.statement_timeout_job_ms, 0)
    connection.exec_driver_sql(f"SET LOCAL statement_timeout = {ms}")


event.listen(_JobSession, "after_begin", _set_job_statement_timeout)

# Sessions for named background jobs (the daily purges in
# app/services/token_cleanup.py): the same engine and pool as requests, but
# every transaction runs under STATEMENT_TIMEOUT_JOB_MS (default 60 s) instead
# of the 5 s request timeout, so a large bounded batch is not cancelled.
JobSessionLocal = async_sessionmaker(
    engine, expire_on_commit=False, sync_session_class=_JobSession
)


# Bounded so a hung DB cannot stall startup on this check.
_ROLE_CHECK_TIMEOUT_SECONDS = 5


async def log_db_role() -> str | None:
    """Log, once at startup, which database role the app is connected as
    (#696). The role name is not a secret. In a hardened environment a role
    other than :data:`APP_DB_ROLE` (e.g. ``DATABASE_URL`` still on the owner)
    is logged as a warning, so a forgotten or rolled-back switch is visible in
    the boot log. Never raises; returns the role name, or None if it could
    not be read (SQLite, DB down)."""
    if _is_sqlite:
        return None
    try:
        async with AsyncSessionLocal() as session:
            result = await asyncio.wait_for(
                session.execute(select(func.current_user())),
                timeout=_ROLE_CHECK_TIMEOUT_SECONDS,
            )
            role = result.scalar_one()
    except Exception as exc:  # noqa: BLE001 - startup must not fail on this
        logger.warning("db_role_check_failed error=%s", type(exc).__name__)
        return None
    if settings.is_hardened and role != APP_DB_ROLE:
        logger.warning("db_role current_user=%s expected=%s", role, APP_DB_ROLE)
    else:
        logger.info("db_role current_user=%s", role)
    return role


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session

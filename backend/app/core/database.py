from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

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
_is_sqlite = settings.async_database_url.startswith("sqlite")

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
    """
    if settings.async_database_url.startswith("sqlite"):
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


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session

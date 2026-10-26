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
_engine_kwargs = (
    {}
    if settings.async_database_url.startswith("sqlite")
    else {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_pre_ping": True,
        "pool_recycle": 1800,
    }
)

engine = create_async_engine(settings.async_database_url, echo=False, **_engine_kwargs)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session

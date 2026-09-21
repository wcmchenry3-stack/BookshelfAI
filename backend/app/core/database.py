from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

# SQLite (local/CI) doesn't support pool sizing knobs; Postgres (Render dev,
# Supabase session pooler in prod) needs a bounded pool with pre-ping so a
# recycled/dropped connection is detected before it's handed to a request
# rather than surfacing as a mid-request error.
_engine_kwargs = (
    {}
    if settings.async_database_url.startswith("sqlite")
    else {
        "pool_size": 5,
        "max_overflow": 5,
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

import os

import pytest

# Ensure DATABASE_URL is a valid-looking URL before any app modules are imported.
# In CI, secrets.TEST_DATABASE_URL is unset so GitHub Actions passes an empty string.
# SQLAlchemy creates the engine lazily — no actual connection is attempted at import
# time, so a non-existent host is safe for unit tests.
_DUMMY_DB_URL = "postgresql+asyncpg://test:test@localhost/test"
if not os.environ.get("DATABASE_URL"):
    os.environ["DATABASE_URL"] = _DUMMY_DB_URL

# Disable Turnstile bot-protection in tests. Production deployments must configure
# TURNSTILE_SECRET_KEY; the startup handler enforces this when TURNSTILE_REQUIRED=true.
if not os.environ.get("TURNSTILE_REQUIRED"):
    os.environ["TURNSTILE_REQUIRED"] = "false"


def pytest_configure(config):
    """Register the 'integration' marker for DB integration tests."""
    config.addinivalue_line(
        "markers", "integration: requires a real PostgreSQL database"
    )


def _has_real_db() -> bool:
    """Return True if DATABASE_URL points to a real (non-dummy) database."""
    url = os.environ.get("DATABASE_URL", "")
    return bool(url) and url != _DUMMY_DB_URL


def pytest_collection_modifyitems(config, items):
    """Auto-skip integration tests when no real database is available."""
    if _has_real_db():
        return
    skip_marker = pytest.mark.skip(reason="No real DATABASE_URL configured")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_marker)


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Clear in-memory rate limit counters before each test.

    Tests run in the same process and share the slowapi MemoryStorage backend.
    Without this fixture, earlier tests eat into the rate limit budget of later
    tests, causing spurious 429 failures.
    """
    from app.core.limiter import limiter

    storage = getattr(getattr(limiter, "_limiter", None), "storage", None)
    if storage and hasattr(storage, "reset"):
        storage.reset()
    yield


@pytest.fixture
async def db_session():
    """Provide an async DB session with rollback-based isolation.

    Uses a savepoint so every test starts and ends with a clean state.
    Only usable when a real DATABASE_URL is configured.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.core.config import settings
    from app.core.database import Base

    engine = create_async_engine(settings.async_database_url, echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        yield session
        await session.rollback()

    await engine.dispose()

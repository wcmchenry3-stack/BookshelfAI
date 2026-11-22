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
    """Auto-skip integration tests when no real database is available.

    With REQUIRE_DB_TESTS=1 (set by CI's test-migrations job) a missing real
    database aborts the run instead, so a mass skip can never pass (#760).
    """
    from tests import _db_guard

    error = _db_guard.missing_db_error()
    if error:
        raise pytest.UsageError(error)
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


@pytest.fixture(autouse=True)
def _reset_jwks_caches():
    """Empty the in-process Google JWKS cache so tests never share keys (#691)."""
    from app.auth.google import google_jwks_cache

    google_jwks_cache.reset()
    yield


@pytest.fixture(scope="session", autouse=True)
def _block_real_ai_providers():
    """Cost guard (#584): any request to a real Anthropic or OpenAI API host
    is blocked for the whole run (``tests/_network_guard.py``)."""
    from tests import _network_guard

    with _network_guard.install():
        yield


@pytest.fixture(autouse=True)
def _no_real_ai_provider_calls():
    """Fail the test if it tried to reach a real AI provider, even if the
    blocking exception was swallowed somewhere."""
    from tests import _network_guard

    _network_guard.attempts.clear()
    yield
    tried = list(_network_guard.attempts)
    _network_guard.attempts.clear()
    if tried:
        pytest.fail(f"real AI provider call attempted: {tried}", pytrace=False)


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


@pytest.fixture(autouse=True)
def _fresh_ai_settings_cache():
    """Give every test a fresh ai_settings cache holding no rows (#526).

    With no rows every key falls back to its env/registry default, read at
    call time, so tests that patch ``settings.scan_enabled`` and friends keep
    working and tests with a fake DB never read the table. Tests that exercise
    the read path call ``ai_settings.reset_cache()`` themselves.
    """
    from app.ai import settings as ai_settings

    ai_settings.reset_cache({})
    yield
    ai_settings.reset_cache()

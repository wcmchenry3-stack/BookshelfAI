"""statement_timeout on real Postgres, through the app's own engine (#696).

- The app engine's sessions run with STATEMENT_TIMEOUT_MS in force.
- The startup parameter (asyncpg ``server_settings``) works on its own, and the
  libpq-style ``?options=-c statement_timeout=...`` DSN form does not (asyncpg
  rejects it), which is why the docs do not ask for it.
- A query that exceeds the timeout, run through ``get_db`` in a route, comes
  back as HTTP 503 + Retry-After and the connection goes back to the pool.
"""

import pytest
import sqlalchemy as sa
from fastapi import Depends, FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from app.core import database
from app.core.config import settings
from app.core.database import (
    AsyncSessionLocal,
    _statement_timeout_connect_args,
    engine,
    get_db,
)
from app.main import app, limiter

pytestmark = pytest.mark.integration

_CURRENT_TIMEOUT_MS = sa.text(
    "SELECT setting FROM pg_settings WHERE name = 'statement_timeout'"
)


@pytest.fixture
async def fresh_app_engine():
    """Drop pooled connections from other tests' event loops before and after."""
    await engine.dispose()
    yield engine
    await engine.dispose()


async def test_app_sessions_run_with_the_configured_timeout(fresh_app_engine):
    assert settings.statement_timeout_ms > 0
    async with AsyncSessionLocal() as session:
        value = (await session.execute(_CURRENT_TIMEOUT_MS)).scalar_one()
    assert value == str(settings.statement_timeout_ms)


async def test_startup_parameter_alone_sets_the_timeout():
    """No checkout listener on this engine: only server_settings applies."""
    probe = create_async_engine(
        settings.async_database_url, connect_args=_statement_timeout_connect_args()
    )
    try:
        for _ in range(2):  # survives check-in / reset-on-return
            async with probe.connect() as conn:
                value = (await conn.execute(_CURRENT_TIMEOUT_MS)).scalar_one()
                assert value == str(settings.statement_timeout_ms)
                await conn.rollback()
    finally:
        await probe.dispose()


async def test_libpq_options_in_the_dsn_are_rejected_by_asyncpg():
    probe = create_async_engine(
        settings.async_database_url + "?options=-c%20statement_timeout%3D5000"
    )
    try:
        with pytest.raises(TypeError, match="options"):
            async with probe.connect():
                pass
    finally:
        await probe.dispose()


async def test_query_over_the_timeout_returns_503_and_frees_the_connection(
    fresh_app_engine, monkeypatch
):
    # The startup parameter was fixed at import; the checkout SET reads the
    # setting each time, so a tiny value here is what the route runs under.
    monkeypatch.setattr(database.settings, "statement_timeout_ms", 50)

    probe = FastAPI()
    probe.state.limiter = limiter
    for exc_class, handler in app.exception_handlers.items():
        probe.add_exception_handler(exc_class, handler)

    @probe.get("/probe/slow")
    @limiter.limit(settings.rate_limit_reads)
    async def slow(request: Request, db=Depends(get_db)):
        await db.execute(sa.text("SELECT pg_sleep(2)"))
        return {"ok": True}

    async with AsyncClient(
        transport=ASGITransport(app=probe), base_url="http://test"
    ) as client:
        response = await client.get("/probe/slow")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_TEMPORARILY_UNAVAILABLE"
    assert response.headers["retry-after"] == "5"

    # The session closed and the connection went back to the pool, usable.
    assert fresh_app_engine.sync_engine.pool.checkedout() == 0
    async with AsyncSessionLocal() as session:
        assert (await session.execute(sa.text("SELECT 1"))).scalar_one() == 1

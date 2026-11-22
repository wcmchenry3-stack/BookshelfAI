"""Regression for #833: a pooled DB connection must not leak into the next
test after the event loop it was opened on has closed.

The two tests are order-dependent on purpose: the first leaves a connection in
the app engine's pool on a loop that is closed when it returns; the second
asserts the autouse ``_drop_stale_db_pool_connections`` fixture gave it an
empty pool. Without the fixture the second test sees the stale connection.
"""

import asyncio

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.integration


def test_leaves_a_pooled_connection_on_a_closed_loop():
    from app.core.database import engine

    async def use_pool():
        async with engine.connect() as conn:
            await conn.execute(select(1))

    asyncio.run(use_pool())
    # The connection went back to the pool; its loop is now closed.
    assert engine.sync_engine.pool.checkedin() == 1


def test_next_test_starts_with_an_empty_pool():
    from app.core.database import engine

    assert engine.sync_engine.pool.checkedin() == 0

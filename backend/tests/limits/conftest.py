"""Fixtures for the scan limits tests (#584). Every test here that touches
the database is marked ``integration`` and skips without a real
DATABASE_URL (``tests/conftest.py``)."""

import pytest

from tests.limits import _harness as h


@pytest.fixture
async def limits_db():
    engine, factory = h.make_factory()
    await h.create_tables(engine)
    db = h.LimitsDb(factory)
    try:
        yield db
    finally:
        await db.cleanup()
        await engine.dispose()


@pytest.fixture
async def ai_settings_table(limits_db):
    """The real ai_settings table, emptied for the test and put back after
    (the table is shared). The cache starts empty, so the first request reads
    the table."""
    from app.ai import settings as ai_settings

    saved = await h.save_and_clear_settings(limits_db.factory)
    ai_settings.reset_cache()
    try:
        yield limits_db
    finally:
        await h.restore_settings(limits_db.factory, saved)
        ai_settings.reset_cache()


@pytest.fixture
def downstream():
    with h.downstream():
        yield

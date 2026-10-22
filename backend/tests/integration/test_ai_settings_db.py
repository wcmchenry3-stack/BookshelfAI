"""ai_settings (#526) against real PostgreSQL: idempotent seed, a DB change
becoming visible after the cache TTL, CLI writes, and the 0006 migration's
upgrade -> check -> downgrade cycle.

Skipped automatically when DATABASE_URL is the dummy URL (see conftest.py).
These tests commit, so the table's rows are saved first and put back after.
"""

import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import delete, inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ai import settings as ai_settings
from app.core.config import settings
from app.core.database import Base
from app.models.ai_settings import AISetting

pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0006_ai_settings.py"
)


@pytest.fixture
async def factory():
    engine = create_async_engine(settings.async_database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        saved = [
            (r.key, r.value, r.updated_by)
            for r in (await db.execute(select(AISetting))).scalars().all()
        ]
        await db.execute(delete(AISetting))
        await db.commit()

    yield factory

    async with factory() as db:
        await db.execute(delete(AISetting))
        for key, value, by in saved:
            db.add(AISetting(key=key, value=value, updated_by=by))
        await db.commit()
    await engine.dispose()


async def _rows(factory) -> dict[str, AISetting]:
    async with factory() as db:
        return await ai_settings.read_rows(db)


async def test_seed_is_idempotent(factory):
    async with factory() as db:
        assert await ai_settings.seed_ai_settings(db) == len(ai_settings.KNOWN_KEYS)
    async with factory() as db:
        assert await ai_settings.seed_ai_settings(db) == 0

    rows = await _rows(factory)
    assert {k: r.value for k, r in rows.items()} == ai_settings.seed_values()
    assert {r.updated_by for r in rows.values()} == {"seed"}
    assert all(r.updated_at is not None for r in rows.values())


async def test_seed_never_overwrites_a_changed_row(factory):
    async with factory() as db:
        await ai_settings.set_setting(
            db, "scan.enhanced.enabled", False, updated_by="ops"
        )
    async with factory() as db:
        inserted = await ai_settings.seed_ai_settings(db)
    assert inserted == len(ai_settings.KNOWN_KEYS) - 1
    row = (await _rows(factory))["scan.enhanced.enabled"]
    assert (row.value, row.updated_by) == (False, "ops")


async def test_startup_seed_uses_the_app_session(factory):
    with patch.object(ai_settings, "_open_session", factory):
        await ai_settings.seed_ai_settings_at_startup()
    assert set(await _rows(factory)) == set(ai_settings.KNOWN_KEYS)


async def test_db_change_visible_after_ttl(factory):
    clock = [5000.0]
    ai_settings.reset_cache()
    with patch.object(ai_settings, "_clock", lambda: clock[0]):
        async with factory() as db:
            await ai_settings.seed_ai_settings(db)
        async with factory() as db:
            assert (await ai_settings.get_ai_settings(db)).max_image_pixels == (
                settings.scan_max_image_pixels
            )

        async with factory() as db:
            await ai_settings.set_setting(
                db, "scan.max_image_pixels", 2_000_000, updated_by="ops"
            )

        clock[0] += settings.ai_settings_cache_ttl_seconds - 0.5
        async with factory() as db:
            cached = await ai_settings.get_ai_settings(db)
        clock[0] += 0.5
        async with factory() as db:
            fresh = await ai_settings.get_ai_settings(db)

    assert cached.max_image_pixels == settings.scan_max_image_pixels
    assert fresh.max_image_pixels == 2_000_000


async def test_read_inside_an_open_transaction_keeps_the_session_usable(factory):
    """The read runs in a SAVEPOINT on the request's session."""
    ai_settings.reset_cache()
    async with factory() as db:
        await ai_settings.set_setting(
            db, "scan.standard.model", "gpt-4o", updated_by="ops"
        )
        await db.execute(select(AISetting.key))  # an open transaction
        current = await ai_settings.get_ai_settings(db)
        assert db.in_transaction()
        assert (await db.execute(select(AISetting.key))).all()
    assert current.spec_for(ai_settings.get_feature("scan.standard")).model == (
        "gpt-4o"
    )


async def test_read_with_no_open_transaction_leaves_none_open(factory):
    """Outside a transaction the read uses a short one of its own, so the
    connection is not left idle in a transaction."""
    ai_settings.reset_cache()
    async with factory() as db:
        await ai_settings.set_setting(
            db, "scan.enhanced.enabled", False, updated_by="ops"
        )
    async with factory() as db:
        assert not db.in_transaction()
        current = await ai_settings.get_ai_settings(db)
        assert not db.in_transaction()
        # The session is still usable afterwards.
        assert (await db.execute(select(AISetting.key))).all()
    assert current.enhanced_scan_enabled is False


async def test_failed_read_does_not_expire_loaded_objects(factory):
    """A session that already holds objects reads through a SAVEPOINT, so a
    failed read does not expire them (no transaction open at the time)."""
    ai_settings.reset_cache()
    async with factory() as db:
        await ai_settings.set_setting(
            db, "scan.enhanced.enabled", True, updated_by="ops"
        )
    async with factory() as db:
        await ai_settings.get_ai_settings(db)  # warm the cache
        row = await db.get(AISetting, "scan.enhanced.enabled")
        await db.commit()
        assert not db.in_transaction()
        with (
            patch.object(ai_settings, "_clock", lambda: 10**9),
            patch.object(db, "execute", side_effect=OSError("db down")),
            pytest.raises(ai_settings.AISettingsUnavailableError),
        ):
            await ai_settings.get_ai_settings(db)
        assert not inspect(row).expired


async def test_invalid_switch_row_is_disabled_and_missing_row_uses_env(factory):
    ai_settings.reset_cache()
    async with factory() as db:
        db.add(AISetting(key="scan.standard.enabled", value="false"))
        await db.commit()
    with patch.object(settings, "scan_enhanced_enabled", True):
        async with factory() as db:
            current = await ai_settings.get_ai_settings(db)
        assert current.scan_enabled is False  # invalid row: fail closed
        assert current.enhanced_scan_enabled is True  # no row: env default


async def test_set_writes_actor_and_rejects_invalid(factory):
    async with factory() as db:
        await ai_settings.set_setting(
            db, "scan.standard.model", "gpt-4o", updated_by="will"
        )
    first = (await _rows(factory))["scan.standard.model"]
    async with factory() as db:
        with pytest.raises(ai_settings.AISettingValueError):
            await ai_settings.set_setting(
                db, "scan.standard.model", "gpt-5", updated_by="will"
            )
        with pytest.raises(ai_settings.AISettingValueError):
            await ai_settings.set_setting(
                db, "scan.max_image_pixels", 100_000_001, updated_by="will"
            )
    async with factory() as db:
        await ai_settings.set_setting(
            db, "scan.standard.model", "gpt-4o-mini", updated_by="ann"
        )
    rows = await _rows(factory)
    second = rows["scan.standard.model"]
    assert "scan.max_image_pixels" not in rows
    assert (first.value, first.updated_by) == ("gpt-4o", "will")
    assert (second.value, second.updated_by) == ("gpt-4o-mini", "ann")
    assert second.updated_at >= first.updated_at


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0006", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_cycle(connection) -> None:
    migration = _load_migration()
    assert (migration.revision, migration.down_revision) == ("0006", "0005")
    context = MigrationContext.configure(connection)

    def has_table() -> bool:
        return inspect(connection).has_table("ai_settings")

    def diffs() -> list:
        return [
            d
            for d in compare_metadata(context, Base.metadata)
            if "ai_settings" in repr(d)
        ]

    with Operations.context(context):
        if has_table():
            migration.downgrade()
        assert not has_table()
        migration.upgrade()  # upgrade
        assert has_table()
        assert diffs() == []  # check: the table matches the ORM model
        migration.downgrade()  # downgrade
        assert not has_table()
        migration.upgrade()  # and back up again
        assert diffs() == []


async def test_migration_upgrade_check_downgrade_cycle(factory):
    """Runs 0006 in a transaction that is rolled back (Postgres DDL is
    transactional), so the shared test database is left as it was."""
    engine = create_async_engine(settings.async_database_url)
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            try:
                await conn.run_sync(_migration_cycle)
            finally:
                await trans.rollback()
    finally:
        await engine.dispose()

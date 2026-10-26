"""Migration 0019 (#540): drops scan_usage_daily and users.enhanced_scan_credits.

The shared test database is built from the models (which no longer have
either), so the cycle starts with the downgrade: it must recreate the table and
the column exactly as 0005 and 0003 made them. Then rows are seeded and the
upgrade must drop both again, leaving no difference to the models (what
``alembic check`` reports). Everything runs in a transaction that is rolled
back (Postgres DDL is transactional), so the database is left as it was.
"""

import importlib.util
import uuid
from datetime import date
from pathlib import Path

import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.core.database import Base

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0019_drop_legacy_scan_counters.py"
)
TABLE = "scan_usage_daily"
INDEX = "ix_scan_usage_daily_usage_date"


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0019", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_cycle(connection) -> None:
    migration = _load_migration()
    assert (migration.revision, migration.down_revision) == ("0019", "0018")
    context = MigrationContext.configure(connection)

    def has_table() -> bool:
        return inspect(connection).has_table(TABLE)

    def user_columns() -> dict:
        return {c["name"]: c for c in inspect(connection).get_columns("users")}

    def diffs() -> list:
        return [
            d
            for d in compare_metadata(context, Base.metadata)
            if TABLE in repr(d) or "enhanced_scan_credits" in repr(d)
        ]

    with Operations.context(context):
        # The models have neither: the current schema has no diff.
        assert not has_table()
        assert "enhanced_scan_credits" not in user_columns()
        assert diffs() == []

        migration.downgrade()

        # Both are back, shaped as 0005 and 0003 made them.
        assert has_table()
        credits = user_columns()["enhanced_scan_credits"]
        assert credits["nullable"] is False
        assert str(credits["default"]) == "5"
        insp = inspect(connection)
        cols = {c["name"]: c for c in insp.get_columns(TABLE)}
        assert set(cols) == {"user_id", "usage_date", "scans"}
        assert all(c["nullable"] is False for c in cols.values())
        assert str(cols["scans"]["default"]) == "0"
        assert insp.get_pk_constraint(TABLE)["constrained_columns"] == [
            "user_id",
            "usage_date",
        ]
        (fk,) = insp.get_foreign_keys(TABLE)
        assert fk["referred_table"] == "users"
        assert fk["constrained_columns"] == ["user_id"]
        assert fk["options"].get("ondelete") == "CASCADE"
        indexes = {i["name"]: i["column_names"] for i in insp.get_indexes(TABLE)}
        assert indexes[INDEX] == ["usage_date"]

        # A user with credits and a counter row: the upgrade drops both.
        uid = uuid.uuid4()
        connection.execute(
            sa.text(
                "INSERT INTO users (id, email, google_id, enhanced_scan_credits) "
                "VALUES (:id, :email, :gid, 3)"
            ),
            {"id": uid, "email": f"{uid}@example.com", "gid": f"g-{uid}"},
        )
        connection.execute(
            sa.text(
                "INSERT INTO scan_usage_daily (user_id, usage_date, scans) "
                "VALUES (:id, :day, 4)"
            ),
            {"id": uid, "day": date(2026, 10, 1)},
        )

        migration.upgrade()

        assert not has_table()
        assert "enhanced_scan_credits" not in user_columns()
        assert diffs() == []
        # The user itself is untouched.
        assert (
            connection.execute(
                sa.text("SELECT count(*) FROM users WHERE id = :id"), {"id": uid}
            ).scalar_one()
            == 1
        )

        # Downgrade restores the schema, not the data.
        migration.downgrade()
        assert has_table()
        assert (
            connection.execute(
                sa.text("SELECT count(*) FROM scan_usage_daily")
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                sa.text("SELECT enhanced_scan_credits FROM users WHERE id = :id"),
                {"id": uid},
            ).scalar_one()
            == 5
        )
        migration.upgrade()
        assert diffs() == []


async def test_migration_0019_upgrade_downgrade_cycle():
    engine = create_async_engine(settings.async_database_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with engine.connect() as conn:
            trans = await conn.begin()
            try:
                await conn.run_sync(_migration_cycle)
            finally:
                await trans.rollback()
    finally:
        await engine.dispose()

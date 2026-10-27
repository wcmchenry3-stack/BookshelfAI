"""Refresh-token families and reuse detection against real Postgres (#690).

- Migration 0022 adds family_id / used_at / replaced_by_id, backfills every
  existing row as its own family (in batches) and drops them again, leaving
  no difference to the models. Runs in a rolled-back transaction.
- Two concurrent refreshes with one token (row lock): exactly one rotation,
  and the loser is not mistaken for reuse (no revocation, no alert).
- A retry within the grace window gets a pair that replaces the successor.
- Reuse after the window revokes every refresh row, bumps token_version and
  sends one Sentry warning.
- The cleanup purge deletes expired and revoked-unrotated rows in batches and
  keeps rotated ones; deleting a successor nulls the pointer to it.

Needs DATABASE_URL pointing at a Postgres database.
"""

import asyncio
import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import jwt
import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth import refresh_family
from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models.refresh_token import RefreshToken
from app.models.user import User
from tests.integration import test_token_version_db as _tv
from tests.unit._sentry_capture import captured_app_events

pytestmark = pytest.mark.integration

# Fixtures and helpers shared with the #689 tests (same engine, users and
# _PausingSession hook for interleaving).
_jwt_keys = _tv._jwt_keys
env = _tv.env
_auth = _tv._auth
_PausingSession = _tv._PausingSession

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0022_refresh_token_families.py"
)
COLUMNS = ("family_id", "used_at", "replaced_by_id")


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0022", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_cycle(connection) -> None:
    migration = _load_migration()
    assert (migration.revision, migration.down_revision) == ("0022", "0021")
    context = MigrationContext.configure(connection)

    def columns() -> dict:
        return {c["name"]: c for c in inspect(connection).get_columns("refresh_tokens")}

    def diffs() -> list:
        return [
            d
            for d in compare_metadata(context, Base.metadata)
            if any(c in repr(d) for c in COLUMNS)
        ]

    with Operations.context(context):
        assert set(COLUMNS) <= set(columns())
        assert diffs() == []

        migration.downgrade()
        assert not set(COLUMNS) & set(columns())

        # Rows from before the migration: each becomes its own family.
        uid = uuid.uuid4()
        connection.execute(
            sa.text("INSERT INTO users (id, email, google_id) VALUES (:id, :e, :g)"),
            {"id": uid, "e": f"{uid}@example.com", "g": f"g-{uid}"},
        )
        for n in range(5):
            connection.execute(
                sa.text(
                    "INSERT INTO refresh_tokens (id, jti, user_id, expires_at) "
                    "VALUES (:id, :jti, :uid, now() + interval '1 day')"
                ),
                {"id": uuid.uuid4(), "jti": f"{uid}-{n}", "uid": uid},
            )

        # A batch of 2 needs three UPDATEs for 5 rows (plus any other rows).
        updates: list[str] = []

        def _count(conn, cursor, statement, *args):
            if statement.lstrip().upper().startswith("UPDATE REFRESH_TOKENS"):
                updates.append(statement)

        sa.event.listen(connection, "before_cursor_execute", _count)
        try:
            with patch.object(migration, "BACKFILL_BATCH_SIZE", 2):
                migration.upgrade()
        finally:
            sa.event.remove(connection, "before_cursor_execute", _count)
        assert len(updates) >= 3

        cols = columns()
        for name in COLUMNS:
            assert cols[name]["nullable"] is True
        assert diffs() == []
        rows = connection.execute(
            sa.text("SELECT id, family_id FROM refresh_tokens WHERE user_id = :u"),
            {"u": uid},
        ).all()
        assert len(rows) == 5
        assert all(family == row_id for row_id, family in rows)
        assert (
            connection.execute(
                sa.text("SELECT count(*) FROM refresh_tokens WHERE family_id IS NULL")
            ).scalar_one()
            == 0
        )
        (fk,) = [
            fk
            for fk in inspect(connection).get_foreign_keys("refresh_tokens")
            if fk["constrained_columns"] == ["replaced_by_id"]
        ]
        assert fk["referred_table"] == "refresh_tokens"
        assert fk["options"].get("ondelete") == "SET NULL"
        assert "ix_refresh_tokens_replaced_by_id" in {
            i["name"] for i in inspect(connection).get_indexes("refresh_tokens")
        }

        # And down again cleanly.
        migration.downgrade()
        assert not set(COLUMNS) & set(columns())
        migration.upgrade()
        assert diffs() == []


async def test_migration_0022_upgrade_downgrade_cycle_with_backfill():
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


def _jti(token: str) -> str:
    return jwt.decode(token, settings.jwt_public_key, algorithms=["RS256"])["jti"]


async def _rows(factory, user_id) -> dict[str, RefreshToken]:
    async with factory() as db:
        rows = (
            (
                await db.execute(
                    select(RefreshToken).where(RefreshToken.user_id == user_id)
                )
            )
            .scalars()
            .all()
        )
    return {r.jti: r for r in rows}


async def _backdate(factory, jti: str, seconds: int) -> None:
    async with factory() as db:
        await db.execute(
            update(RefreshToken)
            .where(RefreshToken.jti == jti)
            .values(used_at=RefreshToken.used_at - timedelta(seconds=seconds))
        )
        await db.commit()


def _warnings(events) -> list:
    return [e for e in events if e.get("level") == "warning"]


async def test_rotation_inherits_family_from_a_row_without_one(env):
    """make_user writes a row the way pre-0022 code did (no family_id): it is
    its own family, and its successor joins it."""
    factory, client, make_user, _ = env
    user, _access, refresh = await make_user()
    resp = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 200, resp.text
    rows = await _rows(factory, user.id)
    root, child = rows[_jti(refresh)], rows[_jti(resp.json()["refresh_token"])]
    assert root.family_id is None
    assert child.family_id == root.id
    assert root.replaced_by_id == child.id
    assert root.used_at is not None and root.revoked_at is not None


async def test_concurrent_refreshes_rotate_once_and_are_not_reuse(env):
    """As test_token_version_db's fork test, plus: the loser is a race, not
    a replay, so nothing is revoked, token_version is unchanged, and no
    alert is sent."""
    factory, client, make_user, _ = env
    user, _access, refresh = await make_user()
    paused = async_sessionmaker(
        factory.kw["bind"], class_=_PausingSession, expire_on_commit=False
    )
    second: list[asyncio.Task] = []
    b_waited: list[bool] = []

    async def interleave():
        _PausingSession.after_user_load = None
        second.append(
            asyncio.create_task(
                client.post("/auth/refresh", json={"refresh_token": refresh})
            )
        )
        done, _ = await asyncio.wait(second, timeout=1)
        b_waited.append(not done)

    async def _paused_db():
        async with paused() as session:
            yield session

    plain_db = app.dependency_overrides[get_db]
    _PausingSession.after_user_load = interleave
    app.dependency_overrides[get_db] = _paused_db
    try:
        with captured_app_events() as events:
            resp_a = await client.post("/auth/refresh", json={"refresh_token": refresh})
            resp_b = await asyncio.wait_for(second[0], timeout=10)
    finally:
        _PausingSession.after_user_load = None
        app.dependency_overrides[get_db] = plain_db

    assert b_waited == [True]
    assert resp_a.status_code == 200, resp_a.text
    assert resp_b.status_code == 401
    assert resp_b.json()["detail"] == "Refresh token revoked or not found"
    assert _warnings(events) == []

    rows = await _rows(factory, user.id)
    assert len(rows) == 2
    live = [r for r in rows.values() if r.revoked_at is None]
    assert len(live) == 1
    assert rows[_jti(refresh)].replaced_by_id == live[0].id
    async with factory() as db:
        assert (await db.get(User, user.id)).token_version == 0
    # The winner's tokens still work.
    me = await client.get("/auth/me", headers=_auth(resp_a.json()["access_token"]))
    assert me.status_code == 200


async def test_grace_window_retry_replaces_successor(env):
    factory, client, make_user, _ = env
    user, _access, refresh = await make_user()
    lost = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert lost.status_code == 200

    retry = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert retry.status_code == 200, retry.text
    new = retry.json()

    rows = await _rows(factory, user.id)
    root = rows[_jti(refresh)]
    lost_row = rows[_jti(lost.json()["refresh_token"])]
    new_row = rows[_jti(new["refresh_token"])]
    assert root.replaced_by_id == lost_row.replaced_by_id == new_row.id
    assert lost_row.used_at is not None and lost_row.revoked_at is not None
    assert new_row.revoked_at is None
    assert new_row.family_id == lost_row.family_id == root.id
    assert [r for r in rows.values() if r.revoked_at is None] == [new_row]

    me = await client.get("/auth/me", headers=_auth(new["access_token"]))
    assert me.status_code == 200
    again = await client.post(
        "/auth/refresh", json={"refresh_token": new["refresh_token"]}
    )
    assert again.status_code == 200


async def test_reuse_after_window_revokes_everything_and_alerts(env):
    factory, client, make_user, _ = env
    user, access, refresh = await make_user()
    rotated = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert rotated.status_code == 200
    window = settings.refresh_reuse_grace_seconds
    await _backdate(factory, _jti(refresh), window + 1)

    with captured_app_events() as events:
        resp = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "refresh_reused"

    rows = await _rows(factory, user.id)
    assert len(rows) == 2
    assert all(r.revoked_at is not None for r in rows.values())
    async with factory() as db:
        assert (await db.get(User, user.id)).token_version == 1
    for token in (access, rotated.json()["access_token"]):
        me = await client.get("/auth/me", headers=_auth(token))
        assert me.status_code == 401
        assert me.json()["detail"] == "token_revoked"

    (alert,) = _warnings(events)
    assert alert["tags"]["user_id"] == str(user.id)
    assert alert["tags"]["family_id"] == str(rows[_jti(refresh)].id)
    text = str(alert)
    for secret in (refresh, rotated.json()["refresh_token"], *rows, user.email):
        assert secret not in text

    # A second replay is not a second alert: everything is already revoked.
    with captured_app_events() as events:
        again = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert again.json()["detail"] == "token_revoked"
    assert _warnings(events) == []


async def test_purge_keeps_rotated_rows_and_nulls_pointers(env):
    factory, _client, make_user, _ = env
    user, _access, _refresh = await make_user()
    now = datetime.now(UTC)
    later = now + timedelta(days=1)

    def row(tag, **kw):
        return RefreshToken(
            id=uuid.uuid4(),
            jti=f"{user.id}-{tag}",
            user_id=user.id,
            expires_at=kw.pop("expires_at", later),
            **kw,
        )

    logged_out = row("logged-out", revoked_at=now)
    rotated = row("rotated", revoked_at=now, used_at=now)
    expired_rotated = row(
        "expired", revoked_at=now, used_at=now, expires_at=now - timedelta(seconds=1)
    )
    live = row("live")
    async with factory() as db:
        db.add_all([logged_out, live, expired_rotated])
        await db.flush()
        rotated.replaced_by_id = logged_out.id
        db.add(rotated)
        await db.commit()

    with patch.object(refresh_family, "PURGE_BATCH_SIZE", 1):
        async with factory() as db:
            deleted = await refresh_family.purge_refresh_tokens(db)
    assert deleted >= 2  # other users' purgeable rows may go too

    rows = await _rows(factory, user.id)
    kept = {jti.removeprefix(f"{user.id}-") for jti in rows}
    # make_user's own live row stays as well.
    assert {"rotated", "live"} <= kept
    assert "logged-out" not in kept and "expired" not in kept
    assert rows[rotated.jti].replaced_by_id is None

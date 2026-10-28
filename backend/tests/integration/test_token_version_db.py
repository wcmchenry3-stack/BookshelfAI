"""users.token_version against real Postgres (#689).

- Migration 0021 adds the column (NOT NULL, default 0) and drops it again,
  leaving no difference to the models. Runs in a rolled-back transaction.
- A token_version bump through POST /auth/logout-all makes the next request
  with the old access token 401 token_revoked, with the same number of SQL
  statements as an accepted request, and stops the old refresh token. Another
  user's sessions are untouched.
- A refresh that interleaves with logout-all leaves no token that works and
  no unrevoked refresh row.
- Two concurrent refreshes with the same refresh token: one rotates, the
  other gets a replacement pair that displaces the first one's (#690, #734
  review S1), and only one session is left live.

Needs DATABASE_URL pointing at a Postgres database.
"""

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, event, inspect, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.auth.jwt import create_access_token, create_refresh_token
from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models.refresh_token import RefreshToken
from app.models.user import User

pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0021_users_token_version.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0021", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_cycle(connection) -> None:
    migration = _load_migration()
    assert (migration.revision, migration.down_revision) == ("0021", "0020")
    context = MigrationContext.configure(connection)

    def columns() -> dict:
        return {c["name"]: c for c in inspect(connection).get_columns("users")}

    def diffs() -> list:
        return [
            d
            for d in compare_metadata(context, Base.metadata)
            if "token_version" in repr(d)
        ]

    with Operations.context(context):
        assert "token_version" in columns()
        assert diffs() == []

        migration.downgrade()
        assert "token_version" not in columns()

        # A user created before the column exists gets 0 on upgrade.
        uid = uuid.uuid4()
        connection.execute(
            sa.text("INSERT INTO users (id, email, google_id) VALUES (:id, :e, :g)"),
            {"id": uid, "e": f"{uid}@example.com", "g": f"g-{uid}"},
        )

        migration.upgrade()
        col = columns()["token_version"]
        assert col["nullable"] is False
        assert str(col["default"]) == "0"
        assert isinstance(col["type"], sa.Integer)
        assert diffs() == []
        assert (
            connection.execute(
                sa.text("SELECT token_version FROM users WHERE id = :id"), {"id": uid}
            ).scalar_one()
            == 0
        )


async def test_migration_0021_upgrade_downgrade_cycle():
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


@pytest.fixture
def _jwt_keys():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        .decode()
    )
    with (
        patch.object(settings, "jwt_private_key", private),
        patch.object(settings, "jwt_public_key", public),
    ):
        yield


@pytest.fixture
async def env(_jwt_keys):
    engine = create_async_engine(settings.async_database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    statements: list[str] = []

    def _count(conn, cursor, statement, *args):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _count)

    async def _db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _db
    created: list[uuid.UUID] = []

    async def make_user() -> tuple[User, str, str]:
        """A user with one session: (user, access token, refresh token)."""
        tag = uuid.uuid4().hex[:10]
        async with factory() as db:
            user = User(email=f"tv-{tag}@example.com", google_id=f"tv-{tag}")
            db.add(user)
            await db.flush()
            refresh, jti = create_refresh_token(str(user.id), user.token_version)
            db.add(
                RefreshToken(
                    jti=jti,
                    user_id=user.id,
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                )
            )
            await db.commit()
        created.append(user.id)
        return user, create_access_token(str(user.id), user.email, 0), refresh

    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    try:
        yield factory, client, make_user, statements
    finally:
        await client.aclose()
        app.dependency_overrides.clear()
        async with factory() as db:
            await db.execute(delete(User).where(User.id.in_(created)))
            await db.commit()
        event.remove(engine.sync_engine, "before_cursor_execute", _count)
        await engine.dispose()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_bump_revokes_old_access_and_refresh_tokens(env):
    factory, client, make_user, statements = env
    user, access, refresh = await make_user()
    _other, other_access, other_refresh = await make_user()
    assert user.token_version == 0

    statements.clear()
    ok = await client.get("/auth/me", headers=_auth(access))
    assert ok.status_code == 200, ok.text
    accepted = len(statements)

    out = await client.post("/auth/logout-all", headers=_auth(access))
    assert out.status_code == 204, out.text

    async with factory() as db:
        assert (await db.get(User, user.id)).token_version == 1
        rows = (
            (
                await db.execute(
                    select(RefreshToken).where(RefreshToken.user_id == user.id)
                )
            )
            .scalars()
            .all()
        )
        assert rows and all(r.revoked_at is not None for r in rows)

    statements.clear()
    revoked = await client.get("/auth/me", headers=_auth(access))
    assert revoked.status_code == 401
    assert revoked.json()["detail"] == "token_revoked"
    # Same statements as an accepted request: the check adds no query.
    assert len(statements) == accepted

    refreshed = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert refreshed.status_code == 401

    # The other user's sessions still work.
    assert (
        await client.get("/auth/me", headers=_auth(other_access))
    ).status_code == 200
    other = await client.post("/auth/refresh", json={"refresh_token": other_refresh})
    assert other.status_code == 200, other.text


async def test_concurrent_bumps_both_count(env):
    import asyncio

    from app.auth.sessions import revoke_all_sessions

    factory, _client, make_user, _statements = env
    user, _access, _refresh = await make_user()

    async def bump():
        async with factory() as db:
            await revoke_all_sessions(db, user)
            await db.commit()

    await asyncio.gather(bump(), bump())
    async with factory() as db:
        assert (await db.get(User, user.id)).token_version == 2


async def test_revoke_all_sessions_refreshes_the_in_memory_user(env):
    from app.auth.sessions import revoke_all_sessions

    factory, _client, make_user, _statements = env
    user, _access, _refresh = await make_user()
    async with factory() as db:
        loaded = await db.get(User, user.id)
        # Someone else bumps first: the loaded row is now stale.
        async with factory() as other:
            await revoke_all_sessions(other, await other.get(User, user.id))
            await other.commit()
        assert loaded.token_version == 0
        await revoke_all_sessions(db, loaded)
        # RETURNING carries the value the row really has, not stale + 1 ...
        assert loaded.token_version == 2
        # ... and it is not dirty, so the commit writes nothing back.
        assert loaded not in db.dirty
        await db.commit()
    async with factory() as db:
        assert (await db.get(User, user.id)).token_version == 2


class _PausingSession(AsyncSession):
    """Runs ``after_user_load`` right after /auth/refresh loads the user
    row, so a test can interleave another transaction at that point."""

    after_user_load = None

    async def get(self, entity, *args, **kwargs):
        obj = await super().get(entity, *args, **kwargs)
        hook = type(self).after_user_load
        if entity is User and hook is not None:
            await hook()
        return obj


async def test_refresh_interleaved_with_logout_all_leaves_nothing_usable(env):
    """A (refresh) has read the refresh row and the user at tv=0; B
    (logout-all) bumps and revokes and commits; A completes the rotation.
    Neither token A could mint is accepted, and no unrevoked refresh row is
    left behind."""
    import asyncio

    from app.auth.sessions import revoke_all_sessions

    factory, client, make_user, _statements = env
    user, _access, refresh = await make_user()
    paused = async_sessionmaker(
        factory.kw["bind"], class_=_PausingSession, expire_on_commit=False
    )
    bumped: list[asyncio.Task] = []

    async def logout_all():
        async with factory() as db:
            await revoke_all_sessions(db, await db.get(User, user.id))
            await db.commit()

    async def interleave():
        _PausingSession.after_user_load = None  # only the first load
        bumped.append(asyncio.create_task(logout_all()))
        # Give B the chance to commit before A goes on. With the user row
        # locked by A, B waits instead and revokes A's new row after A
        # commits; without the lock, B commits here and A's row is orphaned.
        await asyncio.wait(bumped, timeout=1)

    async def _paused_db():
        async with paused() as session:
            yield session

    plain_db = app.dependency_overrides[get_db]
    _PausingSession.after_user_load = interleave
    app.dependency_overrides[get_db] = _paused_db
    try:
        resp = await client.post("/auth/refresh", json={"refresh_token": refresh})
    finally:
        _PausingSession.after_user_load = None
        app.dependency_overrides[get_db] = plain_db
    assert len(bumped) == 1
    await asyncio.wait_for(bumped[0], timeout=10)

    async with factory() as db:
        assert (await db.get(User, user.id)).token_version == 1
        rows = (
            (
                await db.execute(
                    select(RefreshToken).where(RefreshToken.user_id == user.id)
                )
            )
            .scalars()
            .all()
        )
        # No orphan: every refresh row the user has is revoked.
        assert rows and all(r.revoked_at is not None for r in rows)

    # A holds the user lock, so B waits for A to commit: A succeeds, and B
    # then bumps and revokes what A minted.
    assert resp.status_code == 200, resp.text
    minted = resp.json()
    me = await client.get("/auth/me", headers=_auth(minted["access_token"]))
    assert me.status_code == 401
    assert me.json()["detail"] == "token_revoked"
    again = await client.post(
        "/auth/refresh", json={"refresh_token": minted["refresh_token"]}
    )
    assert again.status_code == 401


async def test_concurrent_refreshes_with_one_token_fork_no_session(env):
    """A and B present the same refresh token. A has locked the user row; B
    has already read the refresh row as live and waits for the lock. After A
    rotates and commits, B must see the row is rotated: since #690 (#734
    review S1) B gets a replacement pair that displaces A's, so two 200s,
    and still exactly one live refresh row."""
    import asyncio

    factory, client, make_user, _statements = env
    user, _access, refresh = await make_user()
    paused = async_sessionmaker(
        factory.kw["bind"], class_=_PausingSession, expire_on_commit=False
    )
    second: list[asyncio.Task] = []
    b_waited: list[bool] = []

    async def refresh_b():
        return await client.post("/auth/refresh", json={"refresh_token": refresh})

    async def interleave():
        _PausingSession.after_user_load = None  # only A's load pauses
        second.append(asyncio.create_task(refresh_b()))
        # B reads the (still live) refresh row, then blocks on the user lock
        # A holds; it cannot finish before A commits.
        done, _ = await asyncio.wait(second, timeout=1)
        b_waited.append(not done)

    async def _paused_db():
        async with paused() as session:
            yield session

    plain_db = app.dependency_overrides[get_db]
    _PausingSession.after_user_load = interleave
    app.dependency_overrides[get_db] = _paused_db
    try:
        resp_a = await client.post("/auth/refresh", json={"refresh_token": refresh})
        assert len(second) == 1 and b_waited == [True]
        resp_b = await asyncio.wait_for(second[0], timeout=10)
    finally:
        _PausingSession.after_user_load = None
        app.dependency_overrides[get_db] = plain_db

    assert sorted([resp_a.status_code, resp_b.status_code]) == [200, 200]
    assert resp_a.status_code == 200, resp_a.text
    assert resp_b.status_code == 200, resp_b.text

    async with factory() as db:
        rows = (
            (
                await db.execute(
                    select(RefreshToken).where(RefreshToken.user_id == user.id)
                )
            )
            .scalars()
            .all()
        )
        live = [r for r in rows if r.revoked_at is None]
        assert len(rows) == 3
        assert len(live) == 1
        displaced = [r for r in rows if r.displaced_at is not None]
        assert len(displaced) == 1

"""users.token_version against real Postgres (#689).

- Migration 0021 adds the column (NOT NULL, default 0) and drops it again,
  leaving no difference to the models. Runs in a rolled-back transaction.
- A token_version bump through POST /auth/logout-all makes the next request
  with the old access token 401 token_revoked, with the same number of SQL
  statements as an accepted request, and stops the old refresh token. Another
  user's sessions are untouched.

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
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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

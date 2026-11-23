"""users.ai_consent_at / ai_consent_withdrawn_at against real Postgres (#1023).

- Migration 0031 adds the two nullable ``timestamptz`` columns, existing
  users get NULL (no consent), a re-run after a half-applied upgrade
  resumes, and downgrade removes both, leaving no difference to the models.
  Runs real ``alembic`` commands.
- POST / DELETE /users/me/ai-consent with real tokens change the caller's
  row only, store server time, are idempotent, and GET /auth/me shows the
  state.
- Deleting the user removes the consent with the row (account deletion,
  #291).

Needs DATABASE_URL pointing at a Postgres database.
"""

import asyncio
import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from app.auth.jwt import create_access_token
from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models.user import User
from tests.helpers import bearer, install_jwt_keys, rsa_pem_pair

pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0031_users_ai_consent.py"
)
ALEMBIC_DIR = MIGRATION.parents[1]
COLUMNS = ("ai_consent_at", "ai_consent_withdrawn_at")
PATH = "/users/me/ai-consent"


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0031", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _alembic(action: str, revision: str) -> None:
    # No ini file: env.py would otherwise run logging.config.fileConfig, which
    # disables every existing logger for the rest of the test session.
    config = Config()
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    getattr(command, action)(config, revision)


def _in_sync_connection(fn):
    async def _go():
        engine = create_async_engine(settings.async_database_url)
        try:
            async with engine.connect() as conn:
                return await conn.run_sync(fn)
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def _state(connection) -> dict:
    context = MigrationContext.configure(connection)
    return {
        "revision": context.get_current_revision(),
        "columns": {c["name"]: c for c in inspect(connection).get_columns("users")},
        "diffs": [
            d
            for d in compare_metadata(context, Base.metadata)
            if "ai_consent" in repr(d)
        ],
    }


def _head():
    return _in_sync_connection(
        lambda c: MigrationContext.configure(c).get_current_revision()
    )


def test_migration_0031_upgrade_downgrade_cycle():
    """Real ``alembic`` commands, so the database must be at head (CI runs
    ``alembic upgrade head`` first)."""
    head = _head()
    if head is None:
        pytest.skip("database not migrated with alembic (create_all only)")
    migration = _load_migration()
    assert (migration.revision, migration.down_revision) == ("0031", "0030")

    uid = None
    try:
        state = _in_sync_connection(_state)
        assert set(COLUMNS) <= set(state["columns"])
        assert state["diffs"] == []

        _alembic("downgrade", "0030")
        state = _in_sync_connection(_state)
        assert not set(COLUMNS) & set(state["columns"])

        # A user created before the columns exist has no consent afterwards.
        def _make(connection):
            new = uuid.uuid4()
            connection.execute(
                sa.text("INSERT INTO users (id, email, google_id) VALUES (:i, :e, :g)"),
                {"i": new, "e": f"{new}@example.com", "g": f"g-{new}"},
            )
            connection.commit()
            return new

        uid = _in_sync_connection(_make)

        _alembic("upgrade", head)
        state = _in_sync_connection(_state)
        for name in COLUMNS:
            col = state["columns"][name]
            assert col["nullable"] is True
            assert col["default"] is None
            assert isinstance(col["type"], sa.DateTime)
            assert col["type"].timezone is True
        assert state["diffs"] == []

        def _read(connection):
            return connection.execute(
                sa.text(
                    "SELECT ai_consent_at, ai_consent_withdrawn_at FROM users"
                    " WHERE id = :id"
                ),
                {"id": uid},
            ).one()

        assert tuple(_in_sync_connection(_read)) == (None, None)
    finally:
        _alembic("upgrade", head)
        if uid is not None:

            def _drop(connection):
                connection.execute(
                    sa.text("DELETE FROM users WHERE id = :id"), {"id": uid}
                )
                connection.commit()

            _in_sync_connection(_drop)


def test_migration_0031_resumes_after_a_half_applied_upgrade():
    """One column already there (an earlier attempt), the revision not
    stamped: the re-run adds only the missing one."""
    head = _head()
    if head is None:
        pytest.skip("database not migrated with alembic (create_all only)")
    try:
        _alembic("downgrade", "0030")

        def _half_applied(connection):
            connection.execute(
                sa.text("ALTER TABLE users ADD COLUMN ai_consent_at TIMESTAMPTZ")
            )
            connection.commit()

        _in_sync_connection(_half_applied)
        assert _in_sync_connection(_state)["revision"] == "0030"

        _alembic("upgrade", head)
        state = _in_sync_connection(_state)
        assert state["revision"] == head
        assert set(COLUMNS) <= set(state["columns"])
        assert state["diffs"] == []
    finally:
        _alembic("upgrade", head)


@pytest.fixture
async def env():
    engine = create_async_engine(settings.async_database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _db
    created: list[uuid.UUID] = []

    async def make_user(given: datetime | None = None) -> tuple[uuid.UUID, str]:
        tag = uuid.uuid4().hex[:10]
        async with factory() as db:
            user = User(
                email=f"consent-{tag}@example.com",
                google_id=f"consent-{tag}",
                ai_consent_at=given,
            )
            db.add(user)
            await db.commit()
        created.append(user.id)
        return user.id, create_access_token(str(user.id), user.email, 0)

    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    try:
        with install_jwt_keys(rsa_pem_pair()):
            yield factory, client, make_user
    finally:
        await client.aclose()
        app.dependency_overrides.clear()
        async with factory() as db:
            await db.execute(delete(User).where(User.id.in_(created)))
            await db.commit()
        await engine.dispose()


async def _row(factory, uid) -> User:
    async with factory() as db:
        return await db.get(User, uid)


async def test_grant_withdraw_and_grant_again(env):
    factory, client, make_user = env
    earlier = datetime.now(UTC) - timedelta(days=5)
    alice, alice_token = await make_user()
    bob, bob_token = await make_user(earlier)

    me = await client.get("/auth/me", headers=bearer(alice_token))
    assert me.status_code == 200, me.text
    assert me.json()["ai_consent"] is False

    before = datetime.now(UTC)
    resp = await client.post(PATH, headers=bearer(alice_token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["ai_consent"] is True
    given = (await _row(factory, alice)).ai_consent_at
    assert given >= before - timedelta(seconds=5)

    # Idempotent: the first time is kept.
    again = await client.post(PATH, headers=bearer(alice_token))
    assert again.status_code == 200
    assert (await _row(factory, alice)).ai_consent_at == given

    # Bob is untouched.
    assert (await _row(factory, bob)).ai_consent_at == earlier
    other = await client.get("/auth/me", headers=bearer(bob_token))
    assert other.json()["ai_consent"] is True

    withdrawn = await client.delete(PATH, headers=bearer(alice_token))
    assert withdrawn.status_code == 200
    assert withdrawn.json()["ai_consent"] is False
    row = await _row(factory, alice)
    assert row.ai_consent_at == given
    assert row.ai_consent_withdrawn_at > given
    me = await client.get("/auth/me", headers=bearer(alice_token))
    assert me.json()["ai_consent"] is False

    # Withdrawing again keeps the first withdrawal.
    await client.delete(PATH, headers=bearer(alice_token))
    assert (await _row(factory, alice)).ai_consent_withdrawn_at == (
        row.ai_consent_withdrawn_at
    )

    regranted = await client.post(PATH, headers=bearer(alice_token))
    assert regranted.json()["ai_consent"] is True
    row2 = await _row(factory, alice)
    assert row2.ai_consent_at > row.ai_consent_withdrawn_at
    assert row2.ai_consent_withdrawn_at == row.ai_consent_withdrawn_at


async def test_concurrent_grant_and_withdraw_leave_a_consistent_row(env):
    """The row lock serialises them: whichever runs last wins, and the
    timestamps agree with the outcome."""
    factory, client, make_user = env
    uid, token = await make_user()
    results = await asyncio.gather(
        *(
            (
                client.post(PATH, headers=bearer(token))
                if i % 2 == 0
                else client.delete(PATH, headers=bearer(token))
            )
            for i in range(10)
        )
    )
    assert all(r.status_code == 200 for r in results)
    row = await _row(factory, uid)
    assert row.ai_consent_at is not None
    me = await client.get("/auth/me", headers=bearer(token))
    assert me.json()["ai_consent"] is row.ai_consent


async def test_the_consent_goes_with_the_user_row(env):
    factory, client, make_user = env
    uid, token = await make_user()
    await client.post(PATH, headers=bearer(token))
    async with factory() as db:
        await db.execute(delete(User).where(User.id == uid))
        await db.commit()
    async with factory() as db:
        found = await db.execute(select(User.ai_consent_at).where(User.id == uid))
        assert found.first() is None

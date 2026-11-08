"""users.default_tab against real Postgres (#913).

- Migration 0029 adds the nullable column and ``ck_users_default_tab``
  (validated), existing users get NULL (Scan), and downgrade removes both,
  leaving no difference to the models. Runs real ``alembic`` commands.
- The check constraint refuses any value outside scan / wishlist / my-books.
- PATCH /users/me/preferences with a real token changes the caller's row
  only, and GET /auth/me returns it.

Needs DATABASE_URL pointing at a Postgres database.
"""

import asyncio
import importlib.util
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, inspect
from sqlalchemy.exc import IntegrityError
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
    / "0029_users_default_tab.py"
)
ALEMBIC_DIR = MIGRATION.parents[1]


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0029", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _insert_user(connection, default_tab=None) -> uuid.UUID:
    uid = uuid.uuid4()
    connection.execute(
        sa.text(
            "INSERT INTO users (id, email, google_id) VALUES (:id, :e, :g)"
            if default_tab is None
            else "INSERT INTO users (id, email, google_id, default_tab)"
            " VALUES (:id, :e, :g, :t)"
        ),
        {"id": uid, "e": f"{uid}@example.com", "g": f"g-{uid}", "t": default_tab},
    )
    return uid


def _alembic(action: str, revision: str) -> None:
    """Run ``alembic <action> <revision>`` against the test database, as CI
    and the pre-deploy step do. Migration 0029 validates its CHECK in an
    autocommit block, so it cannot run inside a rolled-back transaction."""
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
    checks = connection.execute(
        sa.text(
            "SELECT convalidated FROM pg_constraint WHERE conname = 'ck_users_default_tab'"
        )
    ).all()
    return {
        "revision": context.get_current_revision(),
        "columns": {c["name"]: c for c in inspect(connection).get_columns("users")},
        "checks": {
            c["name"] for c in inspect(connection).get_check_constraints("users")
        },
        "validated": [row[0] for row in checks],
        "diffs": [
            d
            for d in compare_metadata(context, Base.metadata)
            if "default_tab" in repr(d)
        ],
    }


def test_migration_0029_upgrade_downgrade_cycle():
    """Real ``alembic`` commands, so the database must be at head (CI runs
    ``alembic upgrade head`` first)."""
    head = _in_sync_connection(
        lambda c: MigrationContext.configure(c).get_current_revision()
    )
    if head is None:
        pytest.skip("database not migrated with alembic (create_all only)")
    migration = _load_migration()
    assert (migration.revision, migration.down_revision) == ("0029", "0028")

    uid = None
    try:
        state = _in_sync_connection(_state)
        assert "default_tab" in state["columns"]
        assert "ck_users_default_tab" in state["checks"]
        assert state["diffs"] == []

        _alembic("downgrade", "0028")
        state = _in_sync_connection(_state)
        assert "default_tab" not in state["columns"]
        assert "ck_users_default_tab" not in state["checks"]

        # A user created before the column exists keeps NULL (Scan).
        def _make(connection):
            new = _insert_user(connection)
            connection.commit()
            return new

        uid = _in_sync_connection(_make)

        _alembic("upgrade", head)
        state = _in_sync_connection(_state)
        col = state["columns"]["default_tab"]
        assert col["nullable"] is True
        assert col["default"] is None
        assert isinstance(col["type"], sa.String)
        assert "ck_users_default_tab" in state["checks"]
        # The CHECK is validated, not left NOT VALID.
        assert state["validated"] == [True]
        assert state["diffs"] == []

        def _read(connection):
            return connection.execute(
                sa.text("SELECT default_tab FROM users WHERE id = :id"), {"id": uid}
            ).scalar_one()

        assert _in_sync_connection(_read) is None
    finally:
        _alembic("upgrade", head)
        if uid is not None:

            def _drop(connection):
                connection.execute(
                    sa.text("DELETE FROM users WHERE id = :id"), {"id": uid}
                )
                connection.commit()

            _in_sync_connection(_drop)


def _constraint_checks(connection) -> None:
    for tab in ("scan", "wishlist", "my-books"):
        _insert_user(connection, tab)
    for bad in ("settings", "Scan", "my_books", ""):
        nested = connection.begin_nested()
        with pytest.raises(IntegrityError, match="ck_users_default_tab"):
            _insert_user(connection, bad)
        nested.rollback()


async def test_check_constraint_allows_only_the_three_tabs():
    engine = create_async_engine(settings.async_database_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with engine.connect() as conn:
            trans = await conn.begin()
            try:
                await conn.run_sync(_constraint_checks)
            finally:
                await trans.rollback()
    finally:
        await engine.dispose()


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

    async def make_user(default_tab=None) -> tuple[uuid.UUID, str]:
        tag = uuid.uuid4().hex[:10]
        async with factory() as db:
            user = User(
                email=f"tab-{tag}@example.com",
                google_id=f"tab-{tag}",
                default_tab=default_tab,
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


async def test_patch_changes_only_the_callers_row(env):
    factory, client, make_user = env
    alice, alice_token = await make_user()
    bob, bob_token = await make_user("my-books")

    me = await client.get("/auth/me", headers=bearer(alice_token))
    assert me.status_code == 200, me.text
    assert me.json()["default_tab"] is None

    resp = await client.patch(
        "/users/me/preferences",
        json={"default_tab": "wishlist"},
        headers=bearer(alice_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"default_tab": "wishlist"}

    async with factory() as db:
        assert (await db.get(User, alice)).default_tab == "wishlist"
        assert (await db.get(User, bob)).default_tab == "my-books"

    me = await client.get("/auth/me", headers=bearer(alice_token))
    assert me.json()["default_tab"] == "wishlist"
    other = await client.get("/auth/me", headers=bearer(bob_token))
    assert other.json()["default_tab"] == "my-books"

    cleared = await client.patch(
        "/users/me/preferences",
        json={"default_tab": None},
        headers=bearer(alice_token),
    )
    assert cleared.status_code == 200, cleared.text
    async with factory() as db:
        assert (await db.get(User, alice)).default_tab is None

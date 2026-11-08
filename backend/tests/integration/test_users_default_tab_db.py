"""users.default_tab against real Postgres (#913).

- Migration 0029 adds the nullable column and ``ck_users_default_tab``,
  existing users get NULL (Scan), and downgrade removes both, leaving no
  difference to the models. Runs in a rolled-back transaction.
- The check constraint refuses any value outside scan / wishlist / my-books.
- PATCH /users/me/preferences with a real token changes the caller's row
  only, and GET /auth/me returns it.

Needs DATABASE_URL pointing at a Postgres database.
"""

import importlib.util
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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


def _migration_cycle(connection) -> None:
    migration = _load_migration()
    assert (migration.revision, migration.down_revision) == ("0029", "0028")
    context = MigrationContext.configure(connection)

    def columns() -> dict:
        return {c["name"]: c for c in inspect(connection).get_columns("users")}

    def checks() -> set[str]:
        return {c["name"] for c in inspect(connection).get_check_constraints("users")}

    def diffs() -> list:
        return [
            d
            for d in compare_metadata(context, Base.metadata)
            if "default_tab" in repr(d)
        ]

    with Operations.context(context):
        assert "default_tab" in columns()
        assert "ck_users_default_tab" in checks()
        assert diffs() == []

        migration.downgrade()
        assert "default_tab" not in columns()
        assert "ck_users_default_tab" not in checks()

        # A user created before the column exists keeps NULL (Scan).
        uid = _insert_user(connection)

        migration.upgrade()
        col = columns()["default_tab"]
        assert col["nullable"] is True
        assert col["default"] is None
        assert isinstance(col["type"], sa.String)
        assert "ck_users_default_tab" in checks()
        assert diffs() == []
        assert (
            connection.execute(
                sa.text("SELECT default_tab FROM users WHERE id = :id"), {"id": uid}
            ).scalar_one()
            is None
        )


async def test_migration_0029_upgrade_downgrade_cycle():
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

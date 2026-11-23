"""Library entry versions, ``ETag`` and ``If-Match`` on real Postgres (#685).

- Migration 0032 adds ``version INTEGER NOT NULL DEFAULT 1`` to
  ``user_books`` and ``user_custom_books``: existing rows read as 1, a re-run
  after a half-applied upgrade resumes, and downgrade removes both columns,
  leaving no difference to the models. Runs real ``alembic`` commands.
- Over HTTP with real tokens: every single-entry response carries the
  version in the body and as ``ETag``; ``PATCH`` / ``DELETE`` with the
  current ETag succeed and bump it; a stale one is 412 with the current
  version and changes nothing; a missing one is accepted; a malformed one is
  400; another user's entry is 404 whatever the ETag; no token is 401.
- Two concurrent writers holding the same version: exactly one succeeds,
  the other gets 412, and the winner's write is what is stored.
- The ORM's versioned UPDATE refuses a stale in-memory row on its own
  (``StaleDataError``), without the row lock.

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
from sqlalchemy import delete, inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm.exc import StaleDataError

from alembic import command
from app.auth.jwt import create_access_token
from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models.book import Book
from app.models.user import User
from app.models.user_book import UserBook
from app.models.user_custom_book import UserCustomBook
from tests.helpers import bearer, install_jwt_keys, rsa_pem_pair

pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0032_library_entry_version.py"
)
ALEMBIC_DIR = MIGRATION.parents[1]
TABLES = ("user_books", "user_custom_books")


# ── Migration ────────────────────────────────────────────────────────────────


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0032", MIGRATION)
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
        "columns": {
            table: {c["name"]: c for c in inspect(connection).get_columns(table)}
            for table in TABLES
        },
        "diffs": [
            d for d in compare_metadata(context, Base.metadata) if "version" in repr(d)
        ],
    }


def _head():
    return _in_sync_connection(
        lambda c: MigrationContext.configure(c).get_current_revision()
    )


def _drop_user(uid):
    def _go(connection):
        connection.execute(sa.text("DELETE FROM users WHERE id = :id"), {"id": uid})
        connection.commit()

    _in_sync_connection(_go)


def test_migration_0032_upgrade_downgrade_cycle():
    """Real ``alembic`` commands, so the database must be at head (CI runs
    ``alembic upgrade head`` first)."""
    head = _head()
    if head is None:
        pytest.skip("database not migrated with alembic (create_all only)")
    migration = _load_migration()
    assert (migration.revision, migration.down_revision) == ("0032", "0031")

    uid = None
    try:
        state = _in_sync_connection(_state)
        assert all("version" in state["columns"][t] for t in TABLES)
        assert state["diffs"] == []

        _alembic("downgrade", "0031")
        state = _in_sync_connection(_state)
        assert not any("version" in state["columns"][t] for t in TABLES)

        # An entry written before the column exists reads as version 1.
        def _make(connection):
            new = uuid.uuid4()
            connection.execute(
                sa.text("INSERT INTO users (id, email, google_id) VALUES (:i, :e, :g)"),
                {"i": new, "e": f"{new}@example.com", "g": f"g-{new}"},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO user_custom_books (id, user_id, title, author)"
                    " VALUES (:c, :u, 'Pond Notes', 'A. Local')"
                ),
                {"c": new, "u": new},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO user_books (id, user_id, custom_book_id, status)"
                    " VALUES (:b, :u, :c, 'wishlisted')"
                ),
                {"b": new, "u": new, "c": new},
            )
            connection.commit()
            return new

        uid = _in_sync_connection(_make)

        _alembic("upgrade", head)
        state = _in_sync_connection(_state)
        for table in TABLES:
            col = state["columns"][table]["version"]
            assert col["nullable"] is False
            assert isinstance(col["type"], sa.Integer)
            assert col["default"] == "1"
        assert state["diffs"] == []

        def _read(connection):
            return tuple(
                connection.execute(
                    sa.text(f"SELECT version FROM {table} WHERE id = :id"),
                    {"id": uid},
                ).scalar_one()
                for table in TABLES
            )

        assert _in_sync_connection(_read) == (1, 1)
    finally:
        _alembic("upgrade", head)
        if uid is not None:
            _drop_user(uid)


def test_migration_0032_resumes_after_a_half_applied_upgrade():
    """One table already has the column (an earlier attempt), the revision
    not stamped: the re-run adds only the missing one."""
    head = _head()
    if head is None:
        pytest.skip("database not migrated with alembic (create_all only)")
    try:
        _alembic("downgrade", "0031")

        def _half_applied(connection):
            connection.execute(
                sa.text(
                    "ALTER TABLE user_books ADD COLUMN version INTEGER"
                    " NOT NULL DEFAULT 1"
                )
            )
            connection.commit()

        _in_sync_connection(_half_applied)
        assert _in_sync_connection(_state)["revision"] == "0031"

        _alembic("upgrade", head)
        state = _in_sync_connection(_state)
        assert state["revision"] == head
        assert all("version" in state["columns"][t] for t in TABLES)
        assert state["diffs"] == []
    finally:
        _alembic("upgrade", head)


def test_migration_0032_downgrade_resumes_after_a_half_applied_downgrade():
    """One table already lost the column (an earlier downgrade attempt), the
    revision still 0032: the re-run drops only what is left."""
    head = _head()
    if head is None:
        pytest.skip("database not migrated with alembic (create_all only)")
    try:
        _alembic("downgrade", "0032")

        def _half_downgraded(connection):
            connection.execute(
                sa.text("ALTER TABLE user_custom_books DROP COLUMN version")
            )
            connection.commit()

        _in_sync_connection(_half_downgraded)
        assert _in_sync_connection(_state)["revision"] == "0032"

        _alembic("downgrade", "0031")
        state = _in_sync_connection(_state)
        assert state["revision"] == "0031"
        assert not any("version" in state["columns"][t] for t in TABLES)
    finally:
        _alembic("upgrade", head)


# ── HTTP ─────────────────────────────────────────────────────────────────────


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
    users: list[uuid.UUID] = []
    tag = uuid.uuid4().hex[:10]

    async def make_user() -> tuple[uuid.UUID, dict]:
        uid = uuid.uuid4()
        async with factory() as db:
            db.add(User(id=uid, email=f"{uid}@example.com", google_id=f"g-{uid}"))
            await db.commit()
        users.append(uid)
        token = create_access_token(str(uid), f"{uid}@example.com", 0)
        return uid, bearer(token)

    async def make_entry(uid: uuid.UUID, **fields) -> uuid.UUID:
        async with factory() as db:
            book = Book(
                title=f"Dune {tag}",
                author="Frank Herbert",
                open_library_work_id=f"OL{tag}{uuid.uuid4().hex[:6]}W",
            )
            db.add(book)
            await db.flush()
            entry = UserBook(
                user_id=uid, book_id=book.id, status="wishlisted", **fields
            )
            db.add(entry)
            await db.commit()
            return entry.id

    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    try:
        with install_jwt_keys(rsa_pem_pair()):
            yield factory, client, make_user, make_entry
    finally:
        await client.aclose()
        app.dependency_overrides.clear()
        async with factory() as db:
            await db.execute(delete(UserBook).where(UserBook.user_id.in_(users)))
            await db.execute(
                delete(UserCustomBook).where(UserCustomBook.user_id.in_(users))
            )
            await db.execute(delete(Book).where(Book.title.like(f"%{tag}%")))
            await db.execute(delete(User).where(User.id.in_(users)))
            await db.commit()
        await engine.dispose()


async def _row(factory, entry_id) -> UserBook:
    async with factory() as db:
        return await db.get(UserBook, entry_id)


def _path(entry_id) -> str:
    return f"/user-books/{entry_id}"


async def test_patch_with_the_current_etag_succeeds_and_bumps_it(env):
    factory, client, make_user, make_entry = env
    uid, auth = await make_user()
    entry = await make_entry(uid)

    (listed,) = (await client.get("/user-books", headers=auth)).json()
    assert listed["version"] == 1

    resp = await client.patch(
        _path(entry), json={"notes": "a"}, headers={**auth, "If-Match": '"1"'}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 2
    assert resp.headers["etag"] == '"2"'
    assert (await _row(factory, entry)).version == 2

    # Weak, listed and "*" forms match too.
    for header, expected in (('W/"2"', 3), ('"1", "3"', 4), ("*", 5)):
        resp = await client.patch(
            _path(entry),
            json={"notes": f"n{expected}"},
            headers={**auth, "If-Match": header},
        )
        assert resp.status_code == 200, (header, resp.text)
        assert resp.headers["etag"] == f'"{expected}"'


async def test_a_patch_that_changes_nothing_keeps_the_version(env):
    _, client, make_user, make_entry = env
    uid, auth = await make_user()
    entry = await make_entry(uid, notes="same")
    resp = await client.patch(
        _path(entry), json={"notes": "same"}, headers={**auth, "If-Match": '"1"'}
    )
    assert resp.status_code == 200
    assert resp.headers["etag"] == '"1"'


async def test_a_stale_etag_is_412_and_writes_nothing(env):
    factory, client, make_user, make_entry = env
    uid, auth = await make_user()
    entry = await make_entry(uid)
    first = await client.patch(
        _path(entry), json={"notes": "kept"}, headers={**auth, "If-Match": '"1"'}
    )
    assert first.status_code == 200

    stale = await client.patch(
        _path(entry), json={"notes": "lost"}, headers={**auth, "If-Match": '"1"'}
    )
    assert stale.status_code == 412, stale.text
    assert stale.json() == {
        "detail": {"code": "version_mismatch", "current_version": 2}
    }
    assert stale.headers["etag"] == '"2"'
    row = await _row(factory, entry)
    assert (row.notes, row.version) == ("kept", 2)

    gone = await client.delete(_path(entry), headers={**auth, "If-Match": '"1"'})
    assert gone.status_code == 412
    row = await _row(factory, entry)
    assert row.deleted_at is None and row.version == 2


async def test_missing_if_match_is_accepted(env, caplog):
    _, client, make_user, make_entry = env
    uid, auth = await make_user()
    entry = await make_entry(uid)
    caplog.set_level("INFO", logger="app.api._concurrency")
    resp = await client.patch(_path(entry), json={"rating": 4}, headers=auth)
    assert resp.status_code == 200, resp.text
    assert resp.headers["etag"] == '"2"'
    assert f"if_match: missing on update user_id={uid}" in caplog.messages
    assert all(f"{uid}@example.com" not in m for m in caplog.messages)
    deleted = await client.delete(_path(entry), headers=auth)
    assert deleted.status_code == 204


async def test_malformed_if_match_is_400_and_writes_nothing(env):
    factory, client, make_user, make_entry = env
    uid, auth = await make_user()
    entry = await make_entry(uid)
    for header in ("1", '"one"', "W/1", ""):
        patched = await client.patch(
            _path(entry), json={"notes": "x"}, headers={**auth, "If-Match": header}
        )
        assert patched.status_code == 400, (header, patched.text)
        assert patched.json() == {"detail": "invalid_if_match"}
        deleted = await client.delete(
            _path(entry), headers={**auth, "If-Match": header}
        )
        assert deleted.status_code == 400
    row = await _row(factory, entry)
    assert (row.notes, row.version, row.deleted_at) == (None, 1, None)


async def test_another_users_entry_is_404_whatever_the_etag(env):
    """404 before the version check, so a 412 never reveals that someone
    else's entry exists (or its version)."""
    factory, client, make_user, make_entry = env
    alice, _ = await make_user()
    _, bob_auth = await make_user()
    entry = await make_entry(alice)
    for header in ('"1"', '"7"', "*"):
        headers = {**bob_auth, "If-Match": header}
        patched = await client.patch(_path(entry), json={"notes": "x"}, headers=headers)
        assert patched.status_code == 404, patched.text
        assert "etag" not in patched.headers
        assert (await client.delete(_path(entry), headers=headers)).status_code == 404
    assert (await _row(factory, entry)).version == 1


async def test_no_token_is_401(env):
    _, client, make_user, make_entry = env
    uid, _ = await make_user()
    entry = await make_entry(uid)
    headers = {"If-Match": '"1"'}
    assert (
        await client.patch(_path(entry), json={}, headers=headers)
    ).status_code == 401
    assert (await client.delete(_path(entry), headers=headers)).status_code == 401


async def test_delete_restore_and_adds_carry_the_version(env):
    factory, client, make_user, _ = env
    _, auth = await make_user()
    added = await client.post(
        "/wishlist/unverified",
        json={"title": "Pond Notes", "author": "A. Local"},
        headers=auth,
    )
    assert added.status_code == 201, added.text
    assert added.json()["version"] == 1 and added.headers["etag"] == '"1"'
    entry = added.json()["id"]

    deleted = await client.delete(_path(entry), headers={**auth, "If-Match": '"1"'})
    assert deleted.status_code == 204, deleted.text
    (trashed,) = (await client.get("/user-books/trash", headers=auth)).json()
    assert trashed["version"] == 2

    restored = await client.post(f"{_path(entry)}/restore", headers=auth)
    assert restored.status_code == 200, restored.text
    assert restored.json()["version"] == 3 and restored.headers["etag"] == '"3"'
    # The unverified row is versioned too: trashed, then restored.
    async with factory() as db:
        custom_version = await db.scalar(
            select(UserCustomBook.version)
            .join(UserBook, UserBook.custom_book_id == UserCustomBook.id)
            .where(UserBook.id == uuid.UUID(entry))
        )
    assert custom_version == 3


async def test_concurrent_writers_with_the_same_version(env):
    """Both read version 1. The row lock serialises them: exactly one wins,
    the other gets 412 with the winner's version, and the stored notes are
    the winner's."""
    factory, client, make_user, make_entry = env
    uid, auth = await make_user()
    entry = await make_entry(uid)
    headers = {**auth, "If-Match": '"1"'}
    results = await asyncio.gather(
        *(
            client.patch(_path(entry), json={"notes": f"writer {i}"}, headers=headers)
            for i in range(2)
        )
    )
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 412], [r.text for r in results]
    (winner,) = [r for r in results if r.status_code == 200]
    (loser,) = [r for r in results if r.status_code == 412]
    assert loser.json()["detail"]["current_version"] == 2
    row = await _row(factory, entry)
    assert row.notes == winner.json()["notes"]
    assert row.version == 2


async def test_concurrent_patch_and_delete_with_the_same_version(env):
    """Whichever takes the row lock first wins. PATCH first: the DELETE sees
    version 2 (412). DELETE first: the entry is in the trash, which PATCH
    never reads (404, as before #685)."""
    factory, client, make_user, make_entry = env
    uid, auth = await make_user()
    entry = await make_entry(uid)
    headers = {**auth, "If-Match": '"1"'}
    patched, deleted = await asyncio.gather(
        client.patch(_path(entry), json={"notes": "edit"}, headers=headers),
        client.delete(_path(entry), headers=headers),
    )
    outcome = (patched.status_code, deleted.status_code)
    assert outcome in ((200, 412), (404, 204)), (patched.text, deleted.text)
    row = await _row(factory, entry)
    assert row.version == 2
    if deleted.status_code == 204:
        assert row.deleted_at is not None and row.notes is None
    else:
        assert row.deleted_at is None and row.notes == "edit"


async def test_the_versioned_update_refuses_a_stale_row_on_its_own(env):
    """No row lock here: the ORM writes ``WHERE version = <read version>``,
    so a write from a stale in-memory row matches nothing."""
    factory, _, make_user, make_entry = env
    uid, _ = await make_user()
    entry = await make_entry(uid)
    async with factory() as stale_session:
        stale = await stale_session.get(UserBook, entry)
        async with factory() as other:
            fresh = await other.get(UserBook, entry)
            fresh.notes = "first"
            await other.commit()
        stale.notes = "second"
        with pytest.raises(StaleDataError):
            await stale_session.flush()
        await stale_session.rollback()
    row = await _row(factory, entry)
    assert (row.notes, row.version) == ("first", 2)

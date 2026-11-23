"""``Idempotency-Key`` on real Postgres (#685).

- Over HTTP, through the real app and endpoints with real tokens: a retry
  with the same key replays the first response (``Idempotent-Replayed``)
  and writes nothing new (no second entry, no second audit event); the key
  on a different body is 422; a malformed key is 400; a missing key keeps
  today's behaviour; keys are per user; a 5xx releases the key; a replay
  returns the original response even after the entry's version moved on;
  4xx refusals, 204s and account writes replay; keys expire after 24 h; no
  token is 401 and claims nothing.
- Two concurrent requests with the same key: one effect, one row; the other
  gets 409 ``idempotency_in_progress``. Two concurrent claims on the store
  itself: exactly one wins.
- The purge deletes only rows past 24 h, in batches; deleting a user
  deletes their keys (``ON DELETE CASCADE``).
- On a migrated database: RLS on, the app role's grants, and migration
  0033's upgrade / downgrade cycle (also re-run after a half-applied one).

Needs DATABASE_URL pointing at a Postgres database.
"""

import asyncio
import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import user_books as user_books_api
from app.api._idempotency import HEADER, REPLAYED_HEADER
from app.auth.jwt import create_access_token
from app.core.config import settings
from app.core.database import APP_DB_ROLE, Base, get_db
from app.main import app
from app.models.idempotency_key import IdempotencyKey
from app.models.library_event import LibraryEvent
from app.models.user import User
from app.models.user_book import UserBook
from app.models.user_custom_book import UserCustomBook
from app.services import idempotency as store
from app.services.unverified_books import UnverifiedBookService
from tests.helpers import bearer, install_jwt_keys, rsa_pem_pair
from tests.integration.test_db_role import run_rolled_back

pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0033_idempotency_keys.py"
)
TABLE = "idempotency_keys"
BOOK = {"title": "Pond Notes", "author": "A. Local"}


def _key() -> dict:
    return {HEADER: str(uuid.uuid4())}


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

    async def make_user() -> tuple[uuid.UUID, dict]:
        uid = uuid.uuid4()
        async with factory() as db:
            db.add(User(id=uid, email=f"{uid}@example.com", google_id=f"g-{uid}"))
            await db.commit()
        users.append(uid)
        return uid, bearer(create_access_token(str(uid), f"{uid}@example.com", 0))

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    client = AsyncClient(transport=transport, base_url="http://test")
    try:
        with install_jwt_keys(rsa_pem_pair()):
            yield factory, client, make_user
    finally:
        await client.aclose()
        app.dependency_overrides.clear()
        async with factory() as db:
            await db.execute(
                delete(LibraryEvent).where(LibraryEvent.user_id.in_(users))
            )
            await db.execute(delete(UserBook).where(UserBook.user_id.in_(users)))
            await db.execute(
                delete(UserCustomBook).where(UserCustomBook.user_id.in_(users))
            )
            await db.execute(delete(User).where(User.id.in_(users)))
            await db.commit()
        await engine.dispose()


async def _count(factory, model, uid) -> int:
    async with factory() as db:
        return await db.scalar(
            select(func.count()).select_from(model).where(model.user_id == uid)
        )


async def _keys(factory, uid) -> list[IdempotencyKey]:
    async with factory() as db:
        rows = await db.execute(
            select(IdempotencyKey).where(IdempotencyKey.user_id == uid)
        )
        return list(rows.scalars())


# ── Replay, refusals, scoping ────────────────────────────────────────────────


async def test_a_retried_post_replays_and_creates_nothing_new(env):
    factory, client, make_user = env
    uid, auth = await make_user()
    headers = {**auth, **_key()}
    first = await client.post("/wishlist/unverified", json=BOOK, headers=headers)
    again = await client.post("/wishlist/unverified", json=BOOK, headers=headers)
    assert first.status_code == again.status_code == 201, again.text
    assert again.json() == first.json()
    assert again.headers["etag"] == first.headers["etag"] == '"1"'
    assert again.headers[REPLAYED_HEADER] == "true"
    assert REPLAYED_HEADER.lower() not in first.headers
    assert await _count(factory, UserBook, uid) == 1
    assert await _count(factory, LibraryEvent, uid) == 1
    (row,) = await _keys(factory, uid)
    assert (row.state, row.response_status) == ("complete", 201)
    assert row.response_headers == {"etag": '"1"'}
    assert row.key == headers[HEADER] and len(row.request_hash) == 64


async def test_the_same_key_with_a_different_body_is_422(env):
    factory, client, make_user = env
    uid, auth = await make_user()
    headers = {**auth, **_key()}
    assert (
        await client.post("/wishlist/unverified", json=BOOK, headers=headers)
    ).status_code == 201
    other = {**BOOK, "title": "Another Pond"}
    resp = await client.post("/wishlist/unverified", json=other, headers=headers)
    assert resp.status_code == 422
    assert resp.json() == {"detail": "idempotency_key_reused"}
    # On another endpoint too.
    resp = await client.patch("/users/me/preferences", json={}, headers=headers)
    assert resp.status_code == 422
    assert await _count(factory, UserBook, uid) == 1


async def test_a_malformed_key_is_400_and_writes_nothing(env):
    factory, client, make_user = env
    uid, auth = await make_user()
    for key in ("", "x" * 256, "has space"):
        resp = await client.post(
            "/wishlist/unverified", json=BOOK, headers={**auth, HEADER: key}
        )
        assert resp.status_code == 400, (key, resp.text)
        assert resp.json() == {"detail": "invalid_idempotency_key"}
    assert await _count(factory, UserBook, uid) == 0
    assert await _keys(factory, uid) == []


async def test_a_missing_key_keeps_todays_behaviour(env, caplog):
    factory, client, make_user = env
    uid, auth = await make_user()
    caplog.set_level("INFO", logger="app.api._idempotency")
    first = await client.post("/wishlist/unverified", json=BOOK, headers=auth)
    again = await client.post("/wishlist/unverified", json=BOOK, headers=auth)
    assert (first.status_code, again.status_code) == (201, 409)
    assert again.json() == {"detail": "unverified_duplicate"}
    assert (
        f"idempotency: no key on add_unverified_to_wishlist user_id={uid}"
        in caplog.messages
    )
    assert await _keys(factory, uid) == []


async def test_keys_are_per_user(env):
    factory, client, make_user = env
    alice, alice_auth = await make_user()
    bob, bob_auth = await make_user()
    key = _key()
    mine = await client.post(
        "/wishlist/unverified", json=BOOK, headers={**alice_auth, **key}
    )
    theirs = await client.post(
        "/wishlist/unverified", json=BOOK, headers={**bob_auth, **key}
    )
    assert mine.status_code == theirs.status_code == 201
    assert REPLAYED_HEADER.lower() not in theirs.headers
    assert theirs.json()["id"] != mine.json()["id"]
    assert await _count(factory, UserBook, alice) == 1
    assert await _count(factory, UserBook, bob) == 1


async def test_no_token_is_401_and_claims_nothing(env):
    factory, client, _ = env
    key = _key()
    resp = await client.post("/wishlist/unverified", json=BOOK, headers=key)
    assert resp.status_code == 401
    async with factory() as db:
        claimed = await db.scalar(
            select(func.count())
            .select_from(IdempotencyKey)
            .where(IdempotencyKey.key == key[HEADER])
        )
    assert claimed == 0


async def test_a_5xx_releases_the_key_so_the_retry_runs(env, monkeypatch):
    factory, client, make_user = env
    uid, auth = await make_user()
    headers = {**auth, **_key()}

    async def broken(*_args, **_kwargs):
        raise RuntimeError("database fell over")

    with monkeypatch.context() as patched:
        patched.setattr(UnverifiedBookService, "add", broken)
        failed = await client.post("/wishlist/unverified", json=BOOK, headers=headers)
    assert failed.status_code == 500
    assert await _keys(factory, uid) == []
    retried = await client.post("/wishlist/unverified", json=BOOK, headers=headers)
    assert retried.status_code == 201, retried.text
    assert REPLAYED_HEADER.lower() not in retried.headers
    assert await _count(factory, UserBook, uid) == 1


async def test_a_replay_is_the_original_even_after_the_version_moved(env):
    factory, client, make_user = env
    _, auth = await make_user()
    added = await client.post("/wishlist/unverified", json=BOOK, headers=auth)
    path = f"/user-books/{added.json()['id']}"
    keyed = {**auth, **_key(), "If-Match": '"1"'}
    first = await client.patch(path, json={"notes": "first"}, headers=keyed)
    assert first.status_code == 200 and first.headers["etag"] == '"2"'
    moved = await client.patch(path, json={"notes": "other device"}, headers=auth)
    assert moved.headers["etag"] == '"3"'

    replay = await client.patch(path, json={"notes": "first"}, headers=keyed)
    assert replay.status_code == 200
    assert replay.json() == first.json() and replay.json()["version"] == 2
    assert replay.headers["etag"] == '"2"'
    assert replay.headers[REPLAYED_HEADER] == "true"
    async with factory() as db:
        entry = await db.get(UserBook, uuid.UUID(added.json()["id"]))
    assert (entry.notes, entry.version) == ("other device", 3)


async def test_a_delete_and_a_refusal_replay(env):
    factory, client, make_user = env
    uid, auth = await make_user()
    added = await client.post("/wishlist/unverified", json=BOOK, headers=auth)
    path = f"/user-books/{added.json()['id']}"
    keyed = {**auth, **_key()}
    assert (await client.delete(path, headers=keyed)).status_code == 204
    again = await client.delete(path, headers=keyed)
    assert again.status_code == 204  # not the 404 a second delete would get
    assert again.headers[REPLAYED_HEADER] == "true"

    refused = {**auth, **_key()}
    missing = {"book_id": str(uuid.uuid4())}
    first = await client.post("/purchased", json=missing, headers=refused)
    replay = await client.post("/purchased", json=missing, headers=refused)
    assert first.status_code == replay.status_code == 404
    assert replay.json() == first.json()
    assert replay.headers[REPLAYED_HEADER] == "true"
    states = sorted((r.state, r.response_status) for r in await _keys(factory, uid))
    assert states == [("complete", 204), ("complete", 404)]


async def test_account_writes_replay(env):
    _, client, make_user = env
    _, auth = await make_user()
    consent = {**auth, **_key()}
    first = await client.post("/users/me/ai-consent", headers=consent)
    replay = await client.post("/users/me/ai-consent", headers=consent)
    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json() and replay.json()["ai_consent"] is True
    assert replay.headers[REPLAYED_HEADER] == "true"

    prefs = {**auth, **_key()}
    body = {"default_tab": "wishlist"}
    first = await client.patch("/users/me/preferences", json=body, headers=prefs)
    replay = await client.patch("/users/me/preferences", json=body, headers=prefs)
    assert first.status_code == 200, first.text
    assert replay.json() == first.json()
    assert replay.headers[REPLAYED_HEADER] == "true"

    withdrawn = {**auth, **_key()}
    first = await client.delete("/users/me/ai-consent", headers=withdrawn)
    replay = await client.delete("/users/me/ai-consent", headers=withdrawn)
    assert replay.json() == first.json() and replay.json()["ai_consent"] is False
    assert replay.headers[REPLAYED_HEADER] == "true"


async def test_a_key_expires_after_24_hours(env, monkeypatch):
    factory, client, make_user = env
    uid, auth = await make_user()
    added = await client.post("/wishlist/unverified", json=BOOK, headers=auth)
    path = f"/user-books/{added.json()['id']}"
    keyed = {**auth, **_key()}
    first = await client.patch(path, json={"rating": 4}, headers=keyed)
    assert first.status_code == 200

    later = datetime.now(UTC) + timedelta(hours=23, minutes=59)
    monkeypatch.setattr(store, "utcnow", lambda: later)
    replay = await client.patch(path, json={"rating": 4}, headers=keyed)
    assert replay.headers[REPLAYED_HEADER] == "true"

    expired = datetime.now(UTC) + store.KEY_TTL + timedelta(seconds=1)
    monkeypatch.setattr(store, "utcnow", lambda: expired)
    rerun = await client.patch(path, json={"rating": 4}, headers=keyed)
    assert rerun.status_code == 200
    assert REPLAYED_HEADER.lower() not in rerun.headers
    (row,) = await _keys(factory, uid)
    assert row.created_at == expired  # claimed afresh


# ── A committed write never runs again ──────────────────────────────────────


def _later(monkeypatch, delta):
    at = datetime.now(UTC) + delta
    monkeypatch.setattr(store, "utcnow", lambda: at)


async def _assert_never_rerun(env, uid, headers, monkeypatch):
    """Same-key retries, now and after the pending lease, are refused with
    409 idempotency_outcome_unknown and write nothing."""
    factory, client, _ = env
    for delta in (timedelta(0), store.PENDING_LEASE + timedelta(minutes=1)):
        _later(monkeypatch, delta)
        again = await client.post("/wishlist/unverified", json=BOOK, headers=headers)
        assert again.status_code == 409, again.text
        assert again.json() == {"detail": "idempotency_outcome_unknown"}
    assert await _count(factory, UserBook, uid) == 1
    assert await _count(factory, LibraryEvent, uid) == 1
    (row,) = await _keys(factory, uid)
    assert row.state == "committed" and row.response_status is None


async def test_a_failure_after_the_commit_keeps_the_key(env, monkeypatch):
    """The entry is committed, then building the response fails (as a read
    timing out would): 500, but the key is not released, so the app's retry
    on that 5xx cannot add the book a second time."""
    _, client, make_user = env
    uid, auth = await make_user()
    headers = {**auth, **_key()}

    def broken(_entry):
        raise RuntimeError("statement timeout after the commit")

    with monkeypatch.context() as patched:
        patched.setattr(
            user_books_api, "UserBookRead", SimpleNamespace(model_validate=broken)
        )
        failed = await client.post("/wishlist/unverified", json=BOOK, headers=headers)
    assert failed.status_code == 500
    await _assert_never_rerun(env, uid, headers, monkeypatch)


async def test_a_crash_between_the_commit_and_the_stored_response(env, monkeypatch):
    """The worker dies after the write committed and before the response is
    stored (simulated: storing fails). The row stays committed, and no retry
    re-runs the write, before or after the pending lease."""
    _, client, make_user = env
    uid, auth = await make_user()
    headers = {**auth, **_key()}

    async def died(*_args, **_kwargs):
        raise ConnectionResetError("worker gone")

    with monkeypatch.context() as patched:
        patched.setattr(store, "complete", died)
        first = await client.post("/wishlist/unverified", json=BOOK, headers=headers)
    assert first.status_code == 201
    await _assert_never_rerun(env, uid, headers, monkeypatch)


async def test_a_taken_over_request_can_no_longer_commit_its_write(env, monkeypatch):
    """A request still running past the lease loses its key to a retry; its
    own commit then fails (KeyLostError) and its write is not applied."""
    factory, _, make_user = env
    uid, _ = await make_user()
    async with factory() as old, factory() as new:
        old_id = await store.claim(old, uid, "slow-key", "h" * 64)
        store.arm_commit_marker(old, old_id)
        _later(monkeypatch, store.PENDING_LEASE + timedelta(seconds=1))
        new_id = await store.claim(new, uid, "slow-key", "h" * 64)
        assert isinstance(new_id, int) and new_id != old_id
        old.add(UserCustomBook(user_id=uid, title="Late", author="Writer"))
        with pytest.raises(store.KeyLostError):
            await old.commit()
        await old.rollback()
    assert await _count(factory, UserCustomBook, uid) == 0
    (row,) = await _keys(factory, uid)
    assert (row.id, row.state) == (new_id, "pending")


async def test_the_commit_marks_the_row_in_the_same_transaction(env):
    factory, _, make_user = env
    uid, _ = await make_user()
    async with factory() as db:
        record_id = await store.claim(db, uid, "atomic-key", "h" * 64)
        store.arm_commit_marker(db, record_id)
        db.add(UserCustomBook(user_id=uid, title="Kept", author="Writer"))
        await db.rollback()  # nothing committed: still pending
        (row,) = await _keys(factory, uid)
        assert row.state == "pending"
        db.add(UserCustomBook(user_id=uid, title="Kept", author="Writer"))
        await db.commit()
    (row,) = await _keys(factory, uid)
    assert row.state == "committed"
    assert await _count(factory, UserCustomBook, uid) == 1


# ── Concurrency ──────────────────────────────────────────────────────────────


async def test_two_concurrent_requests_with_one_key_have_one_effect(env, monkeypatch):
    factory, client, make_user = env
    uid, auth = await make_user()
    headers = {**auth, **_key()}
    original = UnverifiedBookService.add

    async def slow(self, *args, **kwargs):
        await asyncio.sleep(0.5)  # hold the claim while the duplicate arrives
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(UnverifiedBookService, "add", slow)
    results = await asyncio.gather(
        *(
            client.post("/wishlist/unverified", json=BOOK, headers=headers)
            for _ in range(2)
        )
    )
    assert sorted(r.status_code for r in results) == [201, 409], [
        r.text for r in results
    ]
    (busy,) = [r for r in results if r.status_code == 409]
    assert busy.json() == {"detail": "idempotency_in_progress"}
    assert busy.headers["retry-after"] == "1"
    assert await _count(factory, UserBook, uid) == 1
    assert await _count(factory, LibraryEvent, uid) == 1
    (row,) = await _keys(factory, uid)
    assert row.state == "complete"

    (winner,) = [r for r in results if r.status_code == 201]
    retry = await client.post("/wishlist/unverified", json=BOOK, headers=headers)
    assert retry.json() == winner.json()
    assert retry.headers[REPLAYED_HEADER] == "true"


async def test_concurrent_claims_on_the_store_have_one_winner(env):
    factory, _, make_user = env
    uid, _ = await make_user()

    async def claim():
        async with factory() as db:
            try:
                return await store.claim(db, uid, "same-key", "h" * 64)
            except store.KeyInProgressError:
                return "in progress"

    outcomes = await asyncio.gather(*(claim() for _ in range(5)))
    winners = [o for o in outcomes if isinstance(o, int)]
    assert len(winners) == 1
    assert outcomes.count("in progress") == 4
    assert len(await _keys(factory, uid)) == 1


# ── Purge and cascade ────────────────────────────────────────────────────────


async def test_the_purge_deletes_only_expired_rows_in_batches(env, monkeypatch):
    factory, _, make_user = env
    uid, _ = await make_user()
    now = datetime.now(UTC)
    ages = [timedelta(hours=h) for h in (25, 30, 48, 72, 23, 1)]
    async with factory() as db:
        db.add_all(
            IdempotencyKey(
                user_id=uid,
                key=f"k{i}",
                request_hash="h" * 64,
                state="complete",
                response_status=200,
                response_body="{}",
                created_at=now - age,
            )
            for i, age in enumerate(ages)
        )
        await db.commit()
    monkeypatch.setattr(store, "PURGE_BATCH_SIZE", 2)
    commits = []
    async with factory() as db:
        real_commit = db.commit

        async def counting_commit():
            commits.append(1)
            await real_commit()

        monkeypatch.setattr(db, "commit", counting_commit)
        purged = await store.purge_expired(db, now)
    assert purged >= 4  # other tests' expired rows may go too
    assert len(commits) >= 3  # 2 + 2 + a short batch
    left = sorted(r.key for r in await _keys(factory, uid))
    assert left == ["k4", "k5"]


async def test_deleting_a_user_deletes_their_keys(env):
    factory, client, make_user = env
    uid, auth = await make_user()
    await client.post("/wishlist/unverified", json=BOOK, headers={**auth, **_key()})
    assert len(await _keys(factory, uid)) == 1
    async with factory() as db:
        await db.execute(delete(UserBook).where(UserBook.user_id == uid))
        await db.execute(delete(UserCustomBook).where(UserCustomBook.user_id == uid))
        await db.execute(delete(LibraryEvent).where(LibraryEvent.user_id == uid))
        await db.execute(delete(User).where(User.id == uid))
        await db.commit()
    assert await _keys(factory, uid) == []


# ── Privileges, RLS, migration (migrated database) ───────────────────────────


def _privileges_and_cascade(connection) -> None:
    def scalar(sql, **params):
        return connection.execute(sa.text(sql), params).scalar()

    assert scalar(f"SELECT relrowsecurity FROM pg_class WHERE relname = '{TABLE}'")
    for role, privilege, held in (
        (APP_DB_ROLE, "SELECT", True),
        (APP_DB_ROLE, "INSERT", True),
        (APP_DB_ROLE, "UPDATE", True),
        (APP_DB_ROLE, "DELETE", True),
        (APP_DB_ROLE, "TRUNCATE", False),
        ("anon", "SELECT", False),
        ("authenticated", "SELECT", False),
    ):
        exists = scalar("SELECT count(*) FROM pg_roles WHERE rolname = :r", r=role)
        if not exists:
            continue  # anon / authenticated exist only on Supabase-like setups
        assert (
            scalar(
                f"SELECT has_table_privilege(:r, 'public.{TABLE}', :p)",
                r=role,
                p=privilege,
            )
            is held
        ), (role, privilege)
    assert scalar(
        f"SELECT has_sequence_privilege(:r, 'public.{TABLE}_id_seq', 'USAGE')",
        r=APP_DB_ROLE,
    )

    # The app role inserts, completes and deletes rows; deleting the account
    # (as the app) cascades.
    uid = uuid.uuid4()
    connection.execute(sa.text(f"SET LOCAL ROLE {APP_DB_ROLE}"))
    connection.execute(
        sa.text("INSERT INTO users (id, email, google_id) VALUES (:id, :e, :g)"),
        {"id": uid, "e": f"{uid}@example.com", "g": f"g-{uid}"},
    )
    connection.execute(
        sa.text(
            f"INSERT INTO {TABLE} (user_id, key, request_hash, state) "
            "VALUES (:u, 'k', :h, 'pending')"
        ),
        {"u": uid, "h": "h" * 64},
    )
    connection.execute(
        sa.text(f"UPDATE {TABLE} SET state = 'complete' WHERE user_id = :u"),
        {"u": uid},
    )
    connection.execute(sa.text("DELETE FROM users WHERE id = :id"), {"id": uid})
    assert scalar(f"SELECT count(*) FROM {TABLE} WHERE user_id = :u", u=uid) == 0
    connection.execute(sa.text("RESET ROLE"))


async def test_rls_grants_and_cascade_for_the_app_role():
    await run_rolled_back(_privileges_and_cascade)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0033", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _diffs(context) -> list:
    return [d for d in compare_metadata(context, Base.metadata) if TABLE in repr(d)]


def _migration_cycle(connection) -> None:
    from alembic.operations import Operations

    migration = _load_migration()
    assert (migration.revision, migration.down_revision) == ("0033", "0032")
    context = MigrationContext.configure(connection)

    def has_table() -> bool:
        return inspect(connection).has_table(TABLE)

    with Operations.context(context):
        migration.downgrade()
        assert not has_table()
        migration.downgrade()  # guarded: a re-run with the table gone
        migration.upgrade()
        assert has_table()
        migration.upgrade()  # guarded: a re-run with the table there
    assert _diffs(context) == []
    assert connection.execute(
        sa.text(f"SELECT relrowsecurity FROM pg_class WHERE relname = '{TABLE}'")
    ).scalar()


async def test_migration_0033_downgrade_and_upgrade():
    await run_rolled_back(_migration_cycle)


def _half_applied(connection) -> None:
    """The table is there but its created_at index is not (an earlier attempt
    stopped between the two): the re-run adds only the index."""
    from alembic.operations import Operations

    migration = _load_migration()
    context = MigrationContext.configure(connection)
    connection.execute(sa.text("DROP INDEX ix_idempotency_keys_created_at"))
    with Operations.context(context):
        migration.upgrade()
    names = {i["name"] for i in inspect(connection).get_indexes(TABLE)}
    assert "ix_idempotency_keys_created_at" in names
    assert _diffs(context) == []


async def test_migration_0033_resumes_after_a_half_applied_upgrade():
    await run_rolled_back(_half_applied)

"""The library audit log on real Postgres (#684).

- Every library mutation, through the real endpoint functions, writes
  exactly one ``library_events`` row in its own transaction: ``POST
  /wishlist``, ``POST /wishlist/unverified``, ``POST /purchased`` (new and
  promoted), ``PATCH`` (status, notes, rating, overrides), ``DELETE`` (with
  a ``purged`` event per evicted trashed copy), restore and the trash purge.
- A refused mutation (404, 409, 422) and a no-op ``PATCH`` write none; a
  failure after the change was flushed rolls the event back with it.
- Notes and override text never reach ``changed_fields``.
- The retention job deletes events older than the window, in bounded
  batches with a commit each, and does nothing while another connection
  holds its advisory lock. It deletes through the ``purge_library_events``
  function (migration 0035, #1042; its own tests are in
  ``test_library_events_purge_db.py``), so these need a migrated database.
- On a migrated database: the app role can insert and read events but not
  update or delete them; deleting a user as the app role anonymises its
  events (``ON DELETE SET NULL``); migration 0030 downgrades and upgrades
  cleanly with RLS on.

Needs DATABASE_URL pointing at a Postgres database.
"""

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi import HTTPException
from sqlalchemy import delete, insert, inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import user_books as user_books_api
from app.core.config import settings
from app.core.database import APP_DB_ROLE, Base
from app.core.logging import request_id_var
from app.models.book import Book
from app.models.library_event import LibraryEvent
from app.models.user import User
from app.models.user_book import UserBook
from app.models.user_custom_book import UserCustomBook
from app.schemas.user_book import (
    PurchasedCreate,
    UnverifiedBookCreate,
    UserBookUpdate,
    WishlistRequest,
)
from app.services import library_events_retention as retention
from app.services import trash
from tests.integration.test_db_role import run_rolled_back

pytestmark = pytest.mark.integration

NOTES = "Private: lent to my sister, coffee stain p. 40"
MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0030_library_events.py"
)


def _req():
    return SimpleNamespace()


@pytest.fixture
async def env():
    engine = create_async_engine(settings.async_database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    users: list[uuid.UUID] = []
    tag = uuid.uuid4().hex[:10]

    async def make_user() -> User:
        uid = uuid.uuid4()
        async with factory() as db:
            db.add(User(id=uid, email=f"{uid}@example.com", google_id=f"g-{uid}"))
            await db.commit()
        users.append(uid)
        return User(id=uid, email=f"{uid}@example.com")

    async def make_book() -> Book:
        async with factory() as db:
            book = Book(
                title=f"Dune {tag}",
                author="Frank Herbert",
                open_library_work_id=f"OL{tag}{uuid.uuid4().hex[:6]}W",
            )
            db.add(book)
            await db.commit()
            return book

    yield SimpleNamespace(
        engine=engine, factory=factory, make_user=make_user, make_book=make_book
    )

    async with factory() as db:
        await db.execute(delete(LibraryEvent).where(LibraryEvent.user_id.in_(users)))
        await db.execute(delete(UserBook).where(UserBook.user_id.in_(users)))
        await db.execute(
            delete(UserCustomBook).where(UserCustomBook.user_id.in_(users))
        )
        await db.execute(delete(Book).where(Book.title.like(f"%{tag}%")))
        await db.execute(delete(User).where(User.id.in_(users)))
        await db.commit()
    await engine.dispose()


async def _events(env, user) -> list[LibraryEvent]:
    async with env.factory() as db:
        rows = await db.execute(
            select(LibraryEvent)
            .where(LibraryEvent.user_id == user.id)
            .order_by(LibraryEvent.id)
        )
        return list(rows.scalars())


async def _entry(env, user, book, **fields) -> uuid.UUID:
    """A library row written directly (no event): test setup only."""
    async with env.factory() as db:
        ub = UserBook(user_id=user.id, book_id=book.id, status="wishlisted", **fields)
        db.add(ub)
        await db.commit()
        return ub.id


async def _wishlist(env, user, book):
    async with env.factory() as db:
        return await user_books_api.add_to_wishlist.__wrapped__(
            request=_req(),
            req=WishlistRequest(
                open_library_work_id=book.open_library_work_id,
                title="Dune",
                author="Frank Herbert",
            ),
            current_user=user,
            db=db,
        )


async def _unverified(env, user, title="Pond Notes"):
    async with env.factory() as db:
        return await user_books_api.add_unverified_to_wishlist.__wrapped__(
            request=_req(),
            req=UnverifiedBookCreate(title=title, author="A. Local"),
            current_user=user,
            db=db,
        )


async def _purchased(env, user, book_id):
    async with env.factory() as db:
        return await user_books_api.add_purchased.__wrapped__(
            request=_req(),
            req=PurchasedCreate(book_id=book_id),
            current_user=user,
            db=db,
        )


async def _patch(env, user, entry_id, **body):
    async with env.factory() as db:
        return await user_books_api.update_user_book.__wrapped__(
            request=_req(),
            user_book_id=entry_id,
            req=UserBookUpdate(**body),
            current_user=user,
            db=db,
        )


async def _delete(env, user, entry_id):
    async with env.factory() as db:
        await user_books_api.delete_user_book.__wrapped__(
            request=_req(), user_book_id=entry_id, current_user=user, db=db
        )


async def _restore(env, user, entry_id):
    async with env.factory() as db:
        return await user_books_api.restore_user_book.__wrapped__(
            request=_req(), user_book_id=entry_id, current_user=user, db=db
        )


async def _refused(coro, status):
    with pytest.raises(HTTPException) as exc:
        await coro
    assert exc.value.status_code == status


async def _raw_changed_fields(env, user) -> str:
    """Every stored ``changed_fields`` of the user, as the database holds it."""
    async with env.factory() as db:
        rows = await db.execute(
            select(sa.cast(LibraryEvent.changed_fields, sa.Text)).where(
                LibraryEvent.user_id == user.id
            )
        )
        return " ".join(rows.scalars())


# ── One event per mutation ───────────────────────────────────────────────────


async def test_wishlist_add_writes_one_created_event_with_the_request_id(env):
    alice = await env.make_user()
    book = await env.make_book()
    token = request_id_var.set("req-684-wishlist")
    try:
        entry = await _wishlist(env, alice, book)
    finally:
        request_id_var.reset(token)
    (event,) = await _events(env, alice)
    assert event.event_type == "created"
    assert event.user_book_id == entry.id
    assert event.request_id == "req-684-wishlist"
    assert event.changed_fields["status"] == {"old": None, "new": "wishlisted"}
    assert event.changed_fields["book_id"] == {"old": None, "new": str(book.id)}
    assert event.created_at is not None


async def test_wishlist_add_refused_as_owned_writes_nothing(env):
    alice = await env.make_user()
    book = await env.make_book()
    await _wishlist(env, alice, book)
    await _refused(_wishlist(env, alice, book), 409)
    assert [e.event_type for e in await _events(env, alice)] == ["created"]


async def test_unverified_add_writes_one_event_without_the_title(env):
    alice = await env.make_user()
    entry = await _unverified(env, alice, title="Secret Diary Title")
    (event,) = await _events(env, alice)
    assert (event.event_type, event.user_book_id) == ("created", entry.id)
    assert "custom_book_id" in event.changed_fields
    assert "Secret Diary Title" not in await _raw_changed_fields(env, alice)
    # A duplicate is refused and writes nothing.
    await _refused(_unverified(env, alice, title="Secret Diary Title"), 409)
    assert len(await _events(env, alice)) == 1


async def test_purchased_new_entry_and_promotion_each_write_one_event(env):
    alice = await env.make_user()
    book = await env.make_book()
    entry = await _purchased(env, alice, book.id)
    (created,) = await _events(env, alice)
    assert (created.event_type, created.user_book_id) == ("created", entry.id)
    assert created.changed_fields["status"]["new"] == "purchased"

    other = await env.make_book()
    wished = await _wishlist(env, alice, other)
    promoted = await _purchased(env, alice, other.id)
    assert promoted.id == wished.id
    *_, last = await _events(env, alice)
    assert (last.event_type, last.user_book_id) == ("status_changed", wished.id)
    assert last.changed_fields["status"] == {"old": "wishlisted", "new": "purchased"}
    assert last.changed_fields["purchased_at"]["old"] is None
    assert len(await _events(env, alice)) == 3


async def test_purchased_of_a_missing_book_writes_nothing(env):
    alice = await env.make_user()
    await _refused(_purchased(env, alice, uuid.uuid4()), 404)
    assert await _events(env, alice) == []


async def test_patch_writes_one_event_per_request(env):
    alice = await env.make_user()
    entry = await _entry(env, alice, await env.make_book())

    await _patch(env, alice, entry, status="reading", rating=4, notes=NOTES)
    (event,) = await _events(env, alice)
    assert event.event_type == "status_changed"
    assert event.changed_fields["status"] == {"old": "wishlisted", "new": "reading"}
    assert event.changed_fields["rating"] == {"old": None, "new": 4}
    assert event.changed_fields["notes"] == "changed"
    assert "started_at" in event.changed_fields

    await _patch(
        env,
        alice,
        entry,
        notes=NOTES + " (edited)",
        title_override="My Own Title",
        cover_url_override="https://covers.example.org/mine.jpg",
    )
    *_, last = await _events(env, alice)
    assert last.event_type == "updated"
    assert last.changed_fields == {
        "notes": "changed",
        "title_override": "changed",
        "cover_url_override": "changed",
    }
    raw = await _raw_changed_fields(env, alice)
    for text in (NOTES, "lent to my sister", "My Own Title", "covers.example.org"):
        assert text not in raw
    assert len(await _events(env, alice)) == 2


async def test_patch_that_changes_nothing_writes_nothing(env):
    alice = await env.make_user()
    entry = await _entry(env, alice, await env.make_book(), rating=3)
    await _patch(env, alice, entry, rating=3, status="wishlisted")
    await _patch(env, alice, entry)
    assert await _events(env, alice) == []


async def test_refused_patches_write_nothing(env):
    alice, bob = await env.make_user(), await env.make_user()
    entry = await _entry(env, alice, await env.make_book())
    await _refused(_patch(env, bob, entry, status="read"), 404)
    await _refused(_patch(env, alice, uuid.uuid4(), status="read"), 404)
    unverified = await _unverified(env, alice)
    await _refused(_patch(env, alice, unverified.id, title_override="X"), 422)
    assert [e.event_type for e in await _events(env, alice)] == ["created"]
    assert await _events(env, bob) == []


async def test_a_failure_after_the_flush_rolls_the_event_back(env):
    """The event is in the change's transaction: when the request fails
    after both were flushed, neither is committed."""
    alice = await env.make_user()
    entry = await _entry(env, alice, await env.make_book())

    async def flush_then_fail(db, user_id, user_book):
        await db.flush()
        raise RuntimeError("database went away")

    with (
        patch.object(user_books_api, "_reread_and_commit", flush_then_fail),
        pytest.raises(RuntimeError),
    ):
        await _patch(env, alice, entry, status="read", notes=NOTES)
    assert await _events(env, alice) == []
    async with env.factory() as db:
        row = await db.get(UserBook, entry)
        assert (row.status, row.notes) == ("wishlisted", None)


async def test_delete_and_restore_write_one_event_each(env):
    alice = await env.make_user()
    entry = await _entry(env, alice, await env.make_book(), notes=NOTES)
    await _delete(env, alice, entry)
    (deleted,) = await _events(env, alice)
    assert (deleted.event_type, deleted.user_book_id) == ("deleted", entry)
    assert deleted.changed_fields["deleted_at"]["old"] is None
    assert deleted.changed_fields["deleted_at"]["new"] is not None

    await _restore(env, alice, entry)
    *_, restored = await _events(env, alice)
    assert (restored.event_type, restored.user_book_id) == ("restored", entry)
    assert restored.changed_fields["deleted_at"]["new"] is None
    assert len(await _events(env, alice)) == 2
    assert NOTES not in await _raw_changed_fields(env, alice)


async def test_refused_delete_and_restore_write_nothing(env):
    alice = await env.make_user()
    book = await env.make_book()
    entry = await _entry(env, alice, book)
    await _refused(_restore(env, alice, entry), 404)  # not in the trash
    await _delete(env, alice, entry)
    await _refused(_delete(env, alice, entry), 404)  # already trashed
    await _entry(env, alice, book)  # a live copy of the same book
    await _refused(_restore(env, alice, entry), 409)
    assert [e.event_type for e in await _events(env, alice)] == ["deleted"]


async def test_unverified_delete_and_restore(env):
    alice = await env.make_user()
    entry = await _unverified(env, alice)
    await _delete(env, alice, entry.id)
    await _restore(env, alice, entry.id)
    assert [e.event_type for e in await _events(env, alice)] == [
        "created",
        "deleted",
        "restored",
    ]


async def test_delete_writes_a_purged_event_for_each_evicted_copy(env):
    alice = await env.make_user()
    book = await env.make_book()
    old = await _entry(env, alice, book)
    await _delete(env, alice, old)
    newer = await _entry(env, alice, book)
    await _delete(env, alice, newer)  # evicts the older, empty trashed copy
    events = [(e.event_type, e.user_book_id) for e in await _events(env, alice)]
    assert events == [("deleted", old), ("deleted", newer), ("purged", old)]
    async with env.factory() as db:
        assert await db.get(UserBook, old) is None


async def test_trash_purge_writes_one_purged_event_per_row(env):
    alice = await env.make_user()
    now = datetime.now(UTC) + timedelta(days=settings.trash_retention_days + 2)
    entries = [await _entry(env, alice, await env.make_book()) for _ in range(3)]
    for entry in entries:
        await _delete(env, alice, entry)
    async with env.factory() as db:
        # Other users' expired trash may be purged too; count only ours.
        await trash.purge_trash(db, now=now)
    purged = [e for e in await _events(env, alice) if e.event_type == "purged"]
    assert sorted(e.user_book_id for e in purged) == sorted(entries)
    assert all(e.request_id is None and e.changed_fields == {} for e in purged)
    # The event outlives the library row.
    async with env.factory() as db:
        for entry in entries:
            assert await db.get(UserBook, entry) is None


# ── Retention ────────────────────────────────────────────────────────────────


async def _seed_events(env, user, ages_in_days, now) -> None:
    async with env.factory() as db:
        await db.execute(
            insert(LibraryEvent),
            [
                {
                    "user_id": user.id,
                    "user_book_id": uuid.uuid4(),
                    "event_type": "updated",
                    "changed_fields": {"rating": {"old": 1, "new": 2}},
                    "created_at": now - timedelta(days=age),
                }
                for age in ages_in_days
            ],
        )
        await db.commit()


async def _require_purge_function(env) -> None:
    """Skip on a database not built by alembic (no purge function), like
    the other migration-dependent tests; a migrated one must have it."""
    async with env.engine.connect() as conn:
        if not await conn.run_sync(lambda c: inspect(c).has_table("alembic_version")):
            pytest.skip("database not built by alembic upgrade head")


async def test_retention_deletes_only_old_events_in_bounded_batches(env):
    await _require_purge_function(env)
    alice = await env.make_user()
    # Ten years back, so the cutoff is older than every event other tests
    # wrote: only the rows seeded here are past it.
    now = datetime.now(UTC) - timedelta(days=3650)
    window = settings.library_events_retention_days
    await _seed_events(env, alice, [window + 1] * 5, now)
    await _seed_events(env, alice, [window - 1, 1], now)

    commits = 0
    session = env.factory()
    real_commit = session.commit

    async def counting_commit():
        nonlocal commits
        commits += 1
        await real_commit()

    session.commit = counting_commit
    with (
        patch.object(retention, "PURGE_BATCH_SIZE", 2),
        patch.object(retention, "PURGE_MAX_BATCHES", 2),
    ):
        async with session as db:
            first = await retention.purge_old_library_events(db, now=now)
        # Bounded: two batches of two this run, one commit each.
        assert (first, commits) == (4, 2)
        async with env.factory() as db:
            assert await retention.purge_old_library_events(db, now=now) == 1
        async with env.factory() as db:
            assert await retention.purge_old_library_events(db, now=now) == 0

    cutoff = retention.retention_cutoff(now)
    kept = [e.created_at for e in await _events(env, alice)]
    assert len(kept) == 2 and all(at >= cutoff for at in kept)
    # Real events (written now) are untouched.
    other = await env.make_user()
    await _wishlist(env, other, await env.make_book())
    async with env.factory() as db:
        await retention.purge_old_library_events(db, now=now)
    assert len(await _events(env, other)) == 1


async def test_retention_does_nothing_while_another_instance_holds_the_lock(env):
    await _require_purge_function(env)
    alice = await env.make_user()
    now = datetime.now(UTC) - timedelta(days=3650)
    await _seed_events(env, alice, [settings.library_events_retention_days + 5], now)
    async with env.engine.connect() as holder:
        await holder.execute(
            sa.select(sa.func.pg_advisory_lock(retention.PURGE_LOCK_KEY))
        )
        try:
            async with env.factory() as db:
                assert await retention.purge_old_library_events(db, now=now) == 0
            assert len(await _events(env, alice)) == 1
        finally:
            await holder.execute(
                sa.select(sa.func.pg_advisory_unlock(retention.PURGE_LOCK_KEY))
            )
            await holder.commit()
    async with env.factory() as db:
        assert await retention.purge_old_library_events(db, now=now) == 1
    assert await _events(env, alice) == []


async def test_retention_lock_differs_from_the_trash_purge_lock():
    assert retention.PURGE_LOCK_KEY != trash.PURGE_LOCK_KEY


# ── Privileges, account deletion, migration (migrated database) ──────────────


def _privileges_and_set_null(connection) -> None:
    def scalar(sql, **params):
        return connection.execute(sa.text(sql), params).scalar()

    for privilege, held in (
        ("SELECT", True),
        ("INSERT", True),
        ("DELETE", False),
        ("UPDATE", False),
        ("TRUNCATE", False),
    ):
        assert (
            scalar(
                "SELECT has_table_privilege(:r, 'public.library_events', :p)",
                r=APP_DB_ROLE,
                p=privilege,
            )
            is held
        ), privilege

    uid = uuid.uuid4()
    connection.execute(sa.text(f"SET LOCAL ROLE {APP_DB_ROLE}"))
    connection.execute(
        sa.text("INSERT INTO users (id, email, google_id) VALUES (:id, :e, :g)"),
        {"id": uid, "e": f"{uid}@example.com", "g": f"g-{uid}"},
    )
    event_id = connection.execute(
        sa.text(
            "INSERT INTO library_events (user_id, user_book_id, event_type) "
            "VALUES (:u, :b, 'created') RETURNING id"
        ),
        {"u": uid, "b": uuid.uuid4()},
    ).scalar()
    # Update is refused for the app role.
    connection.execute(sa.text("SAVEPOINT probe"))
    with pytest.raises(sa.exc.ProgrammingError) as exc:
        connection.execute(
            sa.text("UPDATE library_events SET event_type = 'updated' WHERE id = :i"),
            {"i": event_id},
        )
    assert getattr(exc.value.orig, "sqlstate", None) == "42501"
    connection.execute(sa.text("ROLLBACK TO SAVEPOINT probe"))
    # Deleting the account (as the app) anonymises its events: the SET NULL
    # runs as the table owner, so the missing UPDATE privilege does not block.
    connection.execute(sa.text(f"SET LOCAL ROLE {APP_DB_ROLE}"))
    connection.execute(sa.text("DELETE FROM users WHERE id = :id"), {"id": uid})
    row = connection.execute(
        sa.text("SELECT user_id, event_type FROM library_events WHERE id = :i"),
        {"i": event_id},
    ).one()
    assert tuple(row) == (None, "created")
    connection.execute(sa.text("RESET ROLE"))


async def test_app_role_cannot_update_and_account_deletion_anonymises():
    await run_rolled_back(_privileges_and_set_null)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0030", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_cycle(connection) -> None:
    from alembic.operations import Operations

    migration = _load_migration()
    assert (migration.revision, migration.down_revision) == ("0030", "0029")
    context = MigrationContext.configure(connection)

    def has_table() -> bool:
        return inspect(connection).has_table("library_events")

    def diffs() -> list:
        return [
            d
            for d in compare_metadata(context, Base.metadata)
            if "library_events" in repr(d)
        ]

    with Operations.context(context):
        migration.downgrade()
        assert not has_table()
        migration.upgrade()
    assert has_table()
    assert diffs() == []
    rls = connection.execute(
        sa.text("SELECT relrowsecurity FROM pg_class WHERE relname = 'library_events'")
    ).scalar()
    assert rls is True
    held = connection.execute(
        sa.text("SELECT has_table_privilege(:r, 'public.library_events', 'UPDATE')"),
        {"r": APP_DB_ROLE},
    ).scalar()
    assert held is False


async def test_migration_0030_downgrade_and_upgrade():
    await run_rolled_back(_migration_cycle)

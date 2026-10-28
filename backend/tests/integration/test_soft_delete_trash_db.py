"""Soft delete, trash, restore and purge on real Postgres (#683).

- Migration 0026 down and up, in a rolled-back transaction: the downgrade
  hard-deletes the trash (including a trashed entry of a book the user also
  has live, which would block the full unique constraint, and a trashed
  unverified entry with its ``user_custom_books`` row), keeps live rows and
  restores ``uq_user_books_user_book``; the upgrade brings the columns and
  indexes back with no difference to the models.
- The partial unique index: a trashed entry does not block a live one of
  the same book; two live entries are still refused.
- Through the real endpoint functions: a deleted entry disappears from the
  list, PATCH, DELETE, ``POST /purchased`` (which adds a new entry rather
  than promote it), ``POST /wishlist`` (the book can be added again), the
  scan / search library check and the unverified cap and duplicate check;
  it appears in the trash with ``deleted_at`` and ``purge_after``; restore
  brings it back intact; restore conflicts (a live entry of the same book,
  of a merged work, an unverified duplicate, the unverified cap) are 409s
  that change nothing; another user's entry or a live one is a 404; an
  entry past the window that is not purged yet can still be restored.
- The purge: deletes only rows past the window, with their
  ``user_custom_books``, in bounded batches with a commit each; is
  idempotent; does nothing while another connection holds its advisory
  lock; and logs counts only.

Needs DATABASE_URL pointing at a Postgres database.
"""

import importlib.util
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from sqlalchemy import delete, inspect, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import user_books as user_books_api
from app.core.config import settings
from app.core.database import Base
from app.models.book import Book
from app.models.edition import Edition
from app.models.user import User
from app.models.user_book import UserBook
from app.models.user_custom_book import UserCustomBook
from app.schemas.book import EnrichedBook
from app.schemas.user_book import (
    PurchasedCreate,
    TrashedUserBookRead,
    UnverifiedBookCreate,
    UserBookRead,
    UserBookUpdate,
    WishlistRequest,
)
from app.services import trash
from app.services.deduplication import DeduplicationService

pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0026_soft_delete_trash.py"
)


def _req():
    return SimpleNamespace()


# ── Migration ────────────────────────────────────────────────────────────────


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0026", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_cycle(connection) -> None:
    migration = _load_migration()
    assert (migration.revision, migration.down_revision) == ("0026", "0025")
    context = MigrationContext.configure(connection)

    def columns(table) -> set:
        return {c["name"] for c in inspect(connection).get_columns(table)}

    def indexes() -> set:
        return {i["name"] for i in inspect(connection).get_indexes("user_books")}

    def uniques() -> set:
        return {
            u["name"] for u in inspect(connection).get_unique_constraints("user_books")
        }

    def diffs() -> list:
        return [
            d
            for d in compare_metadata(context, Base.metadata)
            if "deleted_at" in repr(d)
        ]

    def insert(sql, **params):
        connection.execute(sa.text(sql), params)

    uid, book = uuid.uuid4(), uuid.uuid4()
    insert(
        "INSERT INTO users (id, email, google_id, created_at, updated_at) "
        "VALUES (:id, :e, :g, now(), now())",
        id=uid,
        e=f"{uid}@example.com",
        g=f"g-{uid}",
    )
    insert(
        "INSERT INTO books (id, title, author, language, created_at, updated_at) "
        "VALUES (:id, 'T', 'A', 'en', now(), now())",
        id=book,
    )
    live, trashed = uuid.uuid4(), uuid.uuid4()
    for entry, deleted in ((live, None), (trashed, datetime(2026, 1, 1, tzinfo=UTC))):
        insert(
            "INSERT INTO user_books (id, user_id, book_id, status, deleted_at, "
            "created_at, updated_at) VALUES (:id, :u, :b, 'read', :d, now(), now())",
            id=entry,
            u=uid,
            b=book,
            d=deleted,
        )
    custom, custom_entry = uuid.uuid4(), uuid.uuid4()
    insert(
        "INSERT INTO user_custom_books (id, user_id, title, author, deleted_at, "
        "created_at, updated_at) VALUES (:id, :u, 'P', 'A', now(), now(), now())",
        id=custom,
        u=uid,
    )
    insert(
        "INSERT INTO user_books (id, user_id, custom_book_id, status, deleted_at, "
        "created_at, updated_at) VALUES (:id, :u, :c, 'wishlisted', now(), now(), "
        "now())",
        id=custom_entry,
        u=uid,
        c=custom,
    )

    def ids(table) -> set:
        return set(
            connection.execute(
                sa.text(f"SELECT id FROM {table} WHERE user_id = :u"), {"u": uid}
            ).scalars()
        )

    with Operations.context(context):
        assert "deleted_at" in columns("user_books")
        assert "deleted_at" in columns("user_custom_books")
        assert {"uq_user_books_user_book_live", "ix_user_books_deleted_at"} <= (
            indexes()
        )
        assert "uq_user_books_user_book" not in uniques()
        assert diffs() == []

        # Downgrade with a trashed duplicate of a live entry present: the
        # trash goes (documented), the live entry stays, the full
        # constraint comes back.
        migration.downgrade()
        assert "deleted_at" not in columns("user_books")
        assert "deleted_at" not in columns("user_custom_books")
        assert "uq_user_books_user_book" in uniques()
        assert not {"uq_user_books_user_book_live", "ix_user_books_deleted_at"} & (
            indexes()
        )
        assert ids("user_books") == {live}
        assert ids("user_custom_books") == set()

        migration.upgrade()
        assert "deleted_at" in columns("user_books")
        assert "deleted_at" in columns("user_custom_books")
        assert {"uq_user_books_user_book_live", "ix_user_books_deleted_at"} <= (
            indexes()
        )
        assert "uq_user_books_user_book" not in uniques()
        assert diffs() == []
        assert ids("user_books") == {live}


async def test_migration_0026_upgrade_downgrade_cycle():
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


# ── Fixtures and helpers ─────────────────────────────────────────────────────


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

    async def make_book(**fields) -> Book:
        data = {
            "title": f"Dune {tag}",
            "author": "Frank Herbert",
            "open_library_work_id": f"OL{tag}{uuid.uuid4().hex[:6]}W",
            **fields,
        }
        async with factory() as db:
            book = Book(**data)
            db.add(book)
            await db.commit()
            return book

    yield SimpleNamespace(
        engine=engine,
        factory=factory,
        make_user=make_user,
        make_book=make_book,
        tag=tag,
    )

    async with factory() as db:
        await db.execute(delete(UserBook).where(UserBook.user_id.in_(users)))
        await db.execute(
            delete(UserCustomBook).where(UserCustomBook.user_id.in_(users))
        )
        ours = Book.title.like(f"%{tag}%")
        book_ids = select(Book.id).where(ours)
        await db.execute(delete(Edition).where(Edition.book_id.in_(book_ids)))
        await db.execute(delete(Book).where(ours, Book.merged_into_id.isnot(None)))
        await db.execute(delete(Book).where(ours))
        await db.execute(delete(User).where(User.id.in_(users)))
        await db.commit()
    await engine.dispose()


async def _entry(env, user, book, **fields) -> uuid.UUID:
    async with env.factory() as db:
        ub = UserBook(user_id=user.id, book_id=book.id, status="wishlisted", **fields)
        db.add(ub)
        await db.commit()
        return ub.id


async def _list(env, user) -> list[UserBookRead]:
    async with env.factory() as db:
        rows = await user_books_api.list_user_books.__wrapped__(
            request=_req(), status_filter=None, current_user=user, db=db
        )
        return [UserBookRead.model_validate(r) for r in rows]


async def _trash(env, user) -> list[TrashedUserBookRead]:
    async with env.factory() as db:
        rows = await user_books_api.list_trash.__wrapped__(
            request=_req(), current_user=user, db=db
        )
        return [TrashedUserBookRead.model_validate(r) for r in rows]


async def _delete(env, user, entry_id) -> None:
    async with env.factory() as db:
        await user_books_api.delete_user_book.__wrapped__(
            request=_req(), user_book_id=entry_id, current_user=user, db=db
        )


async def _restore(env, user, entry_id) -> UserBookRead:
    async with env.factory() as db:
        row = await user_books_api.restore_user_book.__wrapped__(
            request=_req(), user_book_id=entry_id, current_user=user, db=db
        )
        return UserBookRead.model_validate(row)


async def _add_unverified(env, user, **fields) -> UserBookRead:
    body = {"title": f"Pond {env.tag}", "author": "A. Local", **fields}
    async with env.factory() as db:
        row = await user_books_api.add_unverified_to_wishlist.__wrapped__(
            request=_req(),
            req=UnverifiedBookCreate(**body),
            current_user=user,
            db=db,
        )
        return UserBookRead.model_validate(row)


async def _wishlist(env, user, book) -> UserBookRead:
    async with env.factory() as db:
        row = await user_books_api.add_to_wishlist.__wrapped__(
            request=_req(),
            req=WishlistRequest(
                open_library_work_id=book.open_library_work_id,
                title="Dune",
                author="Frank Herbert",
            ),
            current_user=user,
            db=db,
        )
        return UserBookRead.model_validate(row)


async def _set_deleted_at(env, entry_id, when) -> None:
    async with env.factory() as db:
        custom_id = await db.scalar(
            select(UserBook.custom_book_id).where(UserBook.id == entry_id)
        )
        await db.execute(
            update(UserBook).where(UserBook.id == entry_id).values(deleted_at=when)
        )
        if custom_id is not None:
            await db.execute(
                update(UserCustomBook)
                .where(UserCustomBook.id == custom_id)
                .values(deleted_at=when)
            )
        await db.commit()


async def _expect(coro, status, detail=None):
    with pytest.raises(HTTPException) as exc:
        await coro
    assert exc.value.status_code == status
    if detail is not None:
        assert exc.value.detail == detail


# ── Partial unique index ─────────────────────────────────────────────────────


async def test_trashed_entry_does_not_block_a_live_one_but_two_live_do(env):
    alice = await env.make_user()
    book = await env.make_book()
    first = await _entry(env, alice, book)
    async with env.factory() as db:
        db.add(UserBook(user_id=alice.id, book_id=book.id, status="read"))
        with pytest.raises(IntegrityError):
            await db.commit()
    await _delete(env, alice, first)
    await _entry(env, alice, book)  # the same book again: fine
    async with env.factory() as db:
        db.add(UserBook(user_id=alice.id, book_id=book.id, status="read"))
        with pytest.raises(IntegrityError):
            await db.commit()


# ── Delete hides the entry everywhere ────────────────────────────────────────


async def test_deleted_entry_is_hidden_everywhere_and_in_the_trash(env):
    alice = await env.make_user()
    book = await env.make_book()
    entry = await _entry(env, alice, book, notes="mine", rating=4)
    await _delete(env, alice, entry)

    # Not a hard delete.
    async with env.factory() as db:
        row = await db.get(UserBook, entry)
        assert row is not None and row.deleted_at is not None
    # The list, PATCH, DELETE again.
    assert await _list(env, alice) == []
    async with env.factory() as db:
        await _expect(
            user_books_api.update_user_book.__wrapped__(
                request=_req(),
                user_book_id=entry,
                req=UserBookUpdate(rating=1),
                current_user=alice,
                db=db,
            ),
            404,
        )
    await _expect(_delete(env, alice, entry), 404)
    # The scan / search library check: not owned.
    candidate = EnrichedBook(
        open_library_work_id=book.open_library_work_id,
        title="Dune",
        author="Frank Herbert",
        confidence=0.9,
        editions=[],
    )
    async with env.factory() as db:
        (checked,) = await DeduplicationService().check(db, alice.id, [candidate])
    assert checked.ownership == "not_owned"
    # The trash lists it with its dates.
    (trashed,) = await _trash(env, alice)
    assert trashed.id == entry
    assert (trashed.notes, trashed.rating) == ("mine", 4)
    assert trashed.purge_after == trashed.deleted_at + timedelta(
        days=settings.trash_retention_days
    )
    # Unchanged by the PATCH attempt.
    async with env.factory() as db:
        assert (await db.get(UserBook, entry)).rating == 4


async def test_wishlist_adds_a_trashed_book_again_as_a_new_entry(env):
    alice = await env.make_user()
    book = await env.make_book()
    entry = await _entry(env, alice, book)
    await _delete(env, alice, entry)
    added = await _wishlist(env, alice, book)
    assert added.id != entry
    assert [e.id for e in await _list(env, alice)] == [added.id]
    assert [e.id for e in await _trash(env, alice)] == [entry]
    # Now the restore conflicts.
    await _expect(_restore(env, alice, entry), 409, "restore_conflict")


async def test_purchased_adds_a_new_entry_rather_than_promote_a_trashed_one(env):
    alice = await env.make_user()
    book = await env.make_book()
    entry = await _entry(env, alice, book)
    await _delete(env, alice, entry)
    async with env.factory() as db:
        row = await user_books_api.add_purchased.__wrapped__(
            request=_req(),
            req=PurchasedCreate(book_id=book.id),
            current_user=alice,
            db=db,
        )
        new_id = row.id
    assert new_id != entry
    async with env.factory() as db:
        trashed = await db.get(UserBook, entry)
    assert (trashed.status, trashed.deleted_at is not None) == ("wishlisted", True)


async def test_unverified_cap_and_duplicates_ignore_the_trash(env):
    alice = await env.make_user()
    with patch.object(settings, "unverified_books_per_user_max", 1):
        first = await _add_unverified(env, alice)
        await _expect(_add_unverified(env, alice, title="Other"), 409)
        await _delete(env, alice, first.id)
        # The trashed entry neither counts towards the cap nor is a duplicate.
        again = await _add_unverified(env, alice)
        assert again.id != first.id
        # Restoring the trashed one would now be both; the duplicate wins.
        await _expect(_restore(env, alice, first.id), 409, "restore_conflict")
        await _delete(env, alice, again.id)
        other = await _add_unverified(env, alice, title=f"Other {env.tag}")
        # The cap is full with a different book: unverified_limit_reached.
        await _expect(_restore(env, alice, first.id), 409, "unverified_limit_reached")
        await _delete(env, alice, other.id)
        restored = await _restore(env, alice, first.id)
    assert restored.unverified is True and restored.book.title == f"Pond {env.tag}"
    async with env.factory() as db:
        custom = await db.get(UserCustomBook, first.book.id)
    assert custom.deleted_at is None


# ── Restore ──────────────────────────────────────────────────────────────────


async def test_restore_brings_the_entry_back_intact(env):
    alice = await env.make_user()
    book = await env.make_book(cover_url="https://books.example/c.jpg")
    edition = Edition(book_id=book.id, isbn_13="979" + str(uuid.uuid4().int)[:10])
    async with env.factory() as db:
        db.add(edition)
        await db.commit()
    entry = await _entry(
        env,
        alice,
        book,
        edition_id=edition.id,
        notes="first read in 1999",
        rating=5,
        title_override="Dune (my copy)",
    )
    async with env.factory() as db:
        await db.execute(
            update(UserBook).where(UserBook.id == entry).values(status="read")
        )
        await db.commit()
    (before,) = await _list(env, alice)
    await _delete(env, alice, entry)

    restored = await _restore(env, alice, entry)
    assert restored.model_dump(exclude={"updated_at"}) == before.model_dump(
        exclude={"updated_at"}
    )
    assert restored.book.title == "Dune (my copy)"
    assert restored.edition.id == edition.id
    assert await _trash(env, alice) == []
    (listed,) = await _list(env, alice)
    assert listed.id == entry


async def test_restore_of_a_live_or_another_users_entry_is_404(env):
    alice, bob = await env.make_user(), await env.make_user()
    book = await env.make_book()
    entry = await _entry(env, alice, book)
    await _expect(_restore(env, alice, entry), 404)  # live
    await _delete(env, alice, entry)
    # IDOR: Bob sees none of Alice's trash and cannot restore or delete it.
    assert await _trash(env, bob) == []
    await _expect(_restore(env, bob, entry), 404)
    await _expect(_delete(env, bob, entry), 404)
    assert [t.id for t in await _trash(env, alice)] == [entry]
    await _expect(_restore(env, alice, uuid.uuid4()), 404)


async def test_restore_conflicts_with_a_live_entry_of_a_merged_work(env):
    alice = await env.make_user()
    survivor = await env.make_book()
    merged = await env.make_book(
        title=f"Dune duplicate {env.tag}", merged_into_id=survivor.id
    )
    on_merged = await _entry(env, alice, merged)
    await _delete(env, alice, on_merged)
    await _entry(env, alice, survivor)
    await _expect(_restore(env, alice, on_merged), 409, "restore_conflict")
    async with env.factory() as db:
        assert (await db.get(UserBook, on_merged)).deleted_at is not None

    # And the other way round: a trashed survivor entry, a live merged one.
    bob = await env.make_user()
    on_survivor = await _entry(env, bob, survivor)
    await _delete(env, bob, on_survivor)
    await _entry(env, bob, merged)
    await _expect(_restore(env, bob, on_survivor), 409, "restore_conflict")


async def test_entry_past_the_window_is_restorable_until_purged(env):
    alice = await env.make_user()
    book = await env.make_book()
    entry = await _entry(env, alice, book)
    await _delete(env, alice, entry)
    long_ago = datetime.now(UTC) - timedelta(days=settings.trash_retention_days + 5)
    await _set_deleted_at(env, entry, long_ago)
    (trashed,) = await _trash(env, alice)
    assert trashed.purge_after < datetime.now(UTC)
    restored = await _restore(env, alice, entry)
    assert restored.id == entry


# ── Purge ────────────────────────────────────────────────────────────────────

# The purge runs against every row in the database, so the tests trash their
# rows far in the past and pass a matching ``now``: nothing another test
# trashed recently is ever past this cutoff.
EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


async def _ids(env, model, user) -> set:
    async with env.factory() as db:
        return set(
            (await db.scalars(select(model.id).where(model.user_id == user.id))).all()
        )


async def test_purge_deletes_only_rows_past_the_window_in_batches(env, caplog):
    alice = await env.make_user()
    now = EPOCH + timedelta(days=100)
    old = now - timedelta(days=settings.trash_retention_days + 1)
    recent = now - timedelta(days=settings.trash_retention_days - 1)

    old_entries = []
    for _ in range(4):
        book = await env.make_book()
        e = await _entry(env, alice, book)
        await _set_deleted_at(env, e, old)
        old_entries.append(e)
    unverified = await _add_unverified(env, alice)
    await _set_deleted_at(env, unverified.id, old)
    keep_recent = await _entry(env, alice, await env.make_book())
    await _set_deleted_at(env, keep_recent, recent)
    keep_live = await _entry(env, alice, await env.make_book())

    commits = 0
    session = env.factory()
    real_commit = session.commit

    async def counting_commit():
        nonlocal commits
        commits += 1
        await real_commit()

    session.commit = counting_commit
    with (
        patch.object(trash, "PURGE_BATCH_SIZE", 2),
        patch.object(trash, "PURGE_MAX_BATCHES", 2),
        caplog.at_level(logging.DEBUG),
    ):
        async with session as db:
            first = await trash.purge_trash(db, now=now)
        # Bounded: two batches of two this run, the rest next run.
        assert (first, commits) == (4, 2)
        async with env.factory() as db:
            second = await trash.purge_trash(db, now=now)
        assert second == 1
        async with env.factory() as db:
            again = await trash.purge_trash(db, now=now)
        assert again == 0  # idempotent

    assert await _ids(env, UserBook, alice) == {keep_recent, keep_live}
    # The unverified entry's own row went with its library row.
    assert await _ids(env, UserCustomBook, alice) == set()
    # Counts only: no ids or titles in the log.
    text = caplog.text
    for value in [*map(str, old_entries), str(unverified.id), env.tag]:
        assert value not in text


async def test_purge_does_nothing_while_another_instance_holds_the_lock(env, caplog):
    alice = await env.make_user()
    now = EPOCH + timedelta(days=200)
    entry = await _entry(env, alice, await env.make_book())
    await _set_deleted_at(env, entry, now - timedelta(days=100))

    async with env.engine.connect() as holder:
        await holder.execute(sa.select(sa.func.pg_advisory_lock(trash.PURGE_LOCK_KEY)))
        try:
            with caplog.at_level(logging.INFO, logger="app.services.trash"):
                async with env.factory() as db:
                    assert await trash.purge_trash(db, now=now) == 0
            assert "another instance holds the lock" in caplog.text
            assert await _ids(env, UserBook, alice) == {entry}
        finally:
            await holder.execute(
                sa.select(sa.func.pg_advisory_unlock(trash.PURGE_LOCK_KEY))
            )
            await holder.commit()

    async with env.factory() as db:
        assert await trash.purge_trash(db, now=now) == 1
    assert await _ids(env, UserBook, alice) == set()

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
- Eviction on ``DELETE``: only the newest trashed copy of a book is kept
  (catalog, merged work, unverified by the duplicate key); the backstop cap
  keeps the newest N (default 1,000: a mass delete of 150 books is all
  restorable); it never touches another user's rows; the trash list shows
  exactly the rows eviction keeps, ties included.
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
    # C6: a live entry whose custom book has deleted_at set (inconsistent,
    # but possible by hand) must survive the downgrade; a trashed custom
    # book no library row references (an orphan) goes.
    live_custom, live_custom_entry, orphan = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for cid in (live_custom, orphan):
        insert(
            "INSERT INTO user_custom_books (id, user_id, title, author, "
            "deleted_at, created_at, updated_at) "
            "VALUES (:id, :u, 'Q', 'A', now(), now(), now())",
            id=cid,
            u=uid,
        )
    insert(
        "INSERT INTO user_books (id, user_id, custom_book_id, status, "
        "created_at, updated_at) VALUES (:id, :u, :c, 'read', now(), now())",
        id=live_custom_entry,
        u=uid,
        c=live_custom,
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
        assert ids("user_books") == {live, live_custom_entry}
        assert ids("user_custom_books") == {live_custom}

        migration.upgrade()
        assert "deleted_at" in columns("user_books")
        assert "deleted_at" in columns("user_custom_books")
        assert {"uq_user_books_user_book_live", "ix_user_books_deleted_at"} <= (
            indexes()
        )
        assert "uq_user_books_user_book" not in uniques()
        assert diffs() == []
        assert ids("user_books") == {live, live_custom_entry}


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
        # Trashing the same book again replaces the older trashed copy.
        await _delete(env, alice, again.id)
        await _expect(_restore(env, alice, first.id), 404)
        other = await _add_unverified(env, alice, title=f"Other {env.tag}")
        # The cap is full with a different book: unverified_limit_reached.
        await _expect(_restore(env, alice, again.id), 409, "unverified_limit_reached")
        await _delete(env, alice, other.id)
        restored = await _restore(env, alice, again.id)
    assert restored.unverified is True and restored.book.title == f"Pond {env.tag}"
    async with env.factory() as db:
        custom = await db.get(UserCustomBook, again.book.id)
        assert await db.get(UserCustomBook, first.book.id) is None
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


# ── Review round 1: the trash cap and concurrency ────────────────────────────


async def _outcome(coro):
    """``"ok"`` or the HTTP status the call raised; anything else raises."""
    try:
        await coro
        return "ok"
    except HTTPException as exc:
        return exc.status_code


async def _counts(env, user) -> tuple[int, int, int]:
    """(live user_books, trashed user_books, user_custom_books) of ``user``."""
    async with env.factory() as db:
        rows = (
            await db.execute(
                select(UserBook.deleted_at).where(UserBook.user_id == user.id)
            )
        ).all()
        custom = len(
            (
                await db.scalars(
                    select(UserCustomBook.id).where(UserCustomBook.user_id == user.id)
                )
            ).all()
        )
    live = sum(1 for r in rows if r.deleted_at is None)
    return live, len(rows) - live, custom


async def test_unverified_loops_are_bounded_by_the_backstop_cap(env):
    """Different titles each time: same-book eviction never applies, so the
    backstop cap bounds the trash (newest kept)."""
    alice = await env.make_user()
    deleted = []
    with (
        patch.object(settings, "trash_max_entries_per_user", 3),
        patch.object(settings, "unverified_books_per_user_max", 2),
    ):
        keep = await _add_unverified(env, alice, title=f"Keep {env.tag}")
        for i in range(8):
            added = await _add_unverified(env, alice, title=f"Pond {i} {env.tag}")
            await _delete(env, alice, added.id)
            deleted.append(added.id)
            live, trashed, custom = await _counts(env, alice)
            assert live == 1 and trashed <= 3 and custom == live + trashed
    assert await _counts(env, alice) == (1, 3, 4)
    # The oldest went first: the newest three deletions are kept.
    assert [t.id for t in await _trash(env, alice)] == deleted[::-1][:3]
    assert [e.id for e in await _list(env, alice)] == [keep.id]


# ── Review round 2: one trashed copy per book, the backstop cap ──────────────


async def test_wishlist_delete_loop_on_one_book_keeps_one_trashed_copy(env):
    alice = await env.make_user()
    book = await env.make_book()
    other = await _entry(env, alice, await env.make_book())
    await _delete(env, alice, other)
    deleted = []
    for _ in range(5):
        added = await _wishlist(env, alice, book)
        await _delete(env, alice, added.id)
        deleted.append(added.id)
        # This book's trashed copy plus the other book's.
        assert (await _counts(env, alice))[1] == 2
    # The newest copy of the book, and the other book's entry untouched.
    assert [t.id for t in await _trash(env, alice)] == [deleted[-1], other]
    restored = await _restore(env, alice, deleted[-1])
    assert restored.id == deleted[-1]


async def test_same_book_eviction_covers_a_merged_work(env):
    alice = await env.make_user()
    survivor = await env.make_book()
    merged = await env.make_book(
        title=f"Dune duplicate {env.tag}", merged_into_id=survivor.id
    )
    unrelated = await _entry(env, alice, await env.make_book())
    await _delete(env, alice, unrelated)
    on_merged = await _entry(env, alice, merged)
    await _delete(env, alice, on_merged)
    on_survivor = await _wishlist(env, alice, survivor)
    await _delete(env, alice, on_survivor.id)
    assert [t.id for t in await _trash(env, alice)] == [on_survivor.id, unrelated]
    # And the other way round: a trashed survivor entry, then the merged row.
    bob = await env.make_user()
    first = await _entry(env, bob, survivor)
    await _delete(env, bob, first)
    second = await _entry(env, bob, merged)
    await _delete(env, bob, second)
    assert [t.id for t in await _trash(env, bob)] == [second]


async def test_unverified_same_book_loop_keeps_one_trashed_copy(env):
    alice = await env.make_user()
    with patch.object(settings, "unverified_books_per_user_max", 2):
        keep = await _add_unverified(env, alice, title=f"Keep {env.tag}")
        others = []
        for i in range(2):  # different titles: each kept
            o = await _add_unverified(env, alice, title=f"Other {i} {env.tag}")
            await _delete(env, alice, o.id)
            others.append(o.id)
        isbn = "9780306406157"
        # The same book each time, matching the previous copy by the
        # normalised title and author or by the ISBN.
        variants = [
            {},
            {"title": f"POND, {env.tag}!"},  # casefolded, punctuation
            {"isbn": isbn},
            {"title": f"Renamed {env.tag}", "isbn": isbn},  # same ISBN
            {"title": f"Renamed again {env.tag}", "isbn": isbn},
        ]
        deleted = []
        for fields in variants:
            added = await _add_unverified(env, alice, **fields)
            await _delete(env, alice, added.id)
            deleted.append(added.id)
            live, trashed, custom = await _counts(env, alice)
            assert (live, trashed, custom) == (1, 3, 4)
    assert [t.id for t in await _trash(env, alice)] == [deleted[-1], *others[::-1]]
    assert [e.id for e in await _list(env, alice)] == [keep.id]


async def test_mass_delete_of_150_books_is_all_restorable_at_the_default_cap(env):
    from app.core.config import Settings

    default_cap = Settings.model_fields["trash_max_entries_per_user"].default
    assert default_cap == 1000
    alice = await env.make_user()
    entries = []
    async with env.factory() as db:
        for i in range(150):
            book = Book(
                title=f"Mass {i} {env.tag}",
                author="A",
                open_library_work_id=f"OL{env.tag}m{i}W",
            )
            db.add(book)
            await db.flush()
            ub = UserBook(user_id=alice.id, book_id=book.id, status="read")
            db.add(ub)
            await db.flush()
            entries.append(ub.id)
        await db.commit()
    with patch.object(settings, "trash_max_entries_per_user", default_cap):
        for e in entries:
            await _delete(env, alice, e)
        trashed = await _trash(env, alice)
        assert len(trashed) == 150
        assert {t.id for t in trashed} == set(entries)
        for e in entries:
            await _restore(env, alice, e)
    assert await _counts(env, alice) == (150, 0, 0)


async def test_backstop_cap_keeps_the_newest_entries(env):
    alice = await env.make_user()
    entries = [await _entry(env, alice, await env.make_book()) for _ in range(12)]
    with patch.object(settings, "trash_max_entries_per_user", 10):
        for e in entries:
            await _delete(env, alice, e)
        listed = await _trash(env, alice)
    assert [t.id for t in listed] == entries[::-1][:10]
    assert await _counts(env, alice) == (0, 10, 0)


async def test_eviction_never_touches_another_users_rows(env):
    """R2-2: Bob's trashed rows are older than Alice's and include the same
    book (catalog and unverified). Alice's deletes trigger both same-book
    and cap eviction; Bob's rows and every live row stay."""
    alice, bob = await env.make_user(), await env.make_user()
    book = await env.make_book()
    long_ago = datetime.now(UTC) - timedelta(days=5)

    async def trash_for(user, book_row):
        e = await _entry(env, user, book_row)
        await _delete(env, user, e)
        await _set_deleted_at(env, e, long_ago)
        return e

    bob_same = await trash_for(bob, book)
    bob_unverified = (await _add_unverified(env, bob)).id
    await _delete(env, bob, bob_unverified)
    await _set_deleted_at(env, bob_unverified, long_ago)
    bob_others = [await trash_for(bob, await env.make_book()) for _ in range(3)]
    bob_live = await _entry(env, bob, await env.make_book())
    alice_live = await _entry(env, alice, await env.make_book())
    bob_before = await _ids(env, UserBook, bob)
    bob_custom_before = await _ids(env, UserCustomBook, bob)

    with patch.object(settings, "trash_max_entries_per_user", 2):
        # Same-book eviction (catalog): Alice trashes the same book twice.
        for _ in range(2):
            await _delete(env, alice, (await _wishlist(env, alice, book)).id)
        # Same-book eviction (unverified): the same title as Bob's, twice.
        for _ in range(2):
            await _delete(env, alice, (await _add_unverified(env, alice)).id)
        # Cap eviction: more distinct books than the cap.
        for _ in range(3):
            await _delete(env, alice, await _entry(env, alice, await env.make_book()))

    assert await _ids(env, UserBook, bob) == bob_before
    assert await _ids(env, UserCustomBook, bob) == bob_custom_before
    assert {bob_same, bob_unverified, *bob_others, bob_live} == bob_before
    async with env.factory() as db:
        assert (await db.get(UserBook, bob_live)).deleted_at is None
        assert (await db.get(UserBook, alice_live)).deleted_at is None
    assert await _counts(env, alice) == (1, 2, 0)


async def test_trash_list_shows_exactly_the_rows_eviction_keeps_on_ties(env):
    """R2-3: rows trashed at the same instant are ordered by id, highest
    first, in the list and in eviction alike."""
    alice = await env.make_user()
    entries = [await _entry(env, alice, await env.make_book()) for _ in range(5)]
    same = datetime.now(UTC) - timedelta(hours=1)
    for e in entries:
        await _set_deleted_at(env, e, same)
    with patch.object(settings, "trash_max_entries_per_user", 3):
        listed = [t.id for t in await _trash(env, alice)]
        assert listed == sorted(entries, reverse=True)[:3]
        async with env.factory() as db:
            assert await trash.evict_over_cap(db, alice.id) == 2
            await db.commit()
    assert await _ids(env, UserBook, alice) == set(listed)


async def test_trash_list_never_returns_more_than_the_cap(env):
    """Even with more trashed rows than the cap (written around DELETE, e.g.
    by a release from before the cap), the list stops at the cap, newest
    first."""
    alice = await env.make_user()
    entries = []
    for i in range(5):
        e = await _entry(env, alice, await env.make_book())
        await _set_deleted_at(env, e, datetime.now(UTC) - timedelta(hours=10 - i))
        entries.append(e)
    with patch.object(settings, "trash_max_entries_per_user", 3):
        listed = await _trash(env, alice)
    assert [t.id for t in listed] == entries[::-1][:3]


async def test_purge_never_deletes_a_row_a_restore_is_clearing(env):
    """Connection A clears ``deleted_at`` on a row past the cutoff and has
    not committed when the purge runs; after A commits the row must survive.
    The purge skips the locked row (``SKIP LOCKED``) and its DELETE re-checks
    that the row is still trashed and past the cutoff."""
    import asyncio

    alice = await env.make_user()
    now = EPOCH + timedelta(days=300)
    unverified = await _add_unverified(env, alice)
    await _set_deleted_at(env, unverified.id, now - timedelta(days=100))

    async with env.engine.connect() as a:
        await a.execute(
            update(UserBook).where(UserBook.id == unverified.id).values(deleted_at=None)
        )
        await a.execute(
            update(UserCustomBook)
            .where(UserCustomBook.id == unverified.book.id)
            .values(deleted_at=None)
        )

        async def purge():
            async with env.factory() as db:
                return await trash.purge_trash(db, now=now)

        task = asyncio.create_task(purge())
        await asyncio.sleep(0.5)  # the purge has selected (and maybe blocked)
        await a.commit()
        await asyncio.wait_for(task, timeout=10)

    async with env.factory() as db:
        row = await db.get(UserBook, unverified.id)
        custom = await db.get(UserCustomBook, unverified.book.id)
    assert row is not None and row.deleted_at is None
    assert custom is not None and custom.deleted_at is None


async def test_patch_after_a_concurrent_delete_is_404_and_changes_nothing(env):
    """A DELETE holds the row (trashing it, not yet committed) when a PATCH
    arrives: the PATCH waits for the row lock, then finds the entry trashed."""
    import asyncio

    alice = await env.make_user()
    entry = await _entry(env, alice, await env.make_book(), rating=2)

    async with env.engine.connect() as deleter:
        await deleter.execute(
            update(UserBook)
            .where(UserBook.id == entry)
            .values(deleted_at=datetime.now(UTC))
        )

        async def patch_it():
            async with env.factory() as db:
                return await user_books_api.update_user_book.__wrapped__(
                    request=_req(),
                    user_book_id=entry,
                    req=UserBookUpdate(rating=5),
                    current_user=alice,
                    db=db,
                )

        task = asyncio.create_task(_outcome(patch_it()))
        await asyncio.sleep(0.5)
        await deleter.commit()
        assert await asyncio.wait_for(task, timeout=10) == 404

    async with env.factory() as db:
        row = await db.get(UserBook, entry)
    assert row.deleted_at is not None and row.rating == 2


async def test_two_concurrent_deletes_are_204_and_404(env):
    import asyncio

    alice = await env.make_user()
    for _ in range(5):
        entry = await _entry(env, alice, await env.make_book())
        outcomes = await asyncio.gather(
            _outcome(_delete(env, alice, entry)), _outcome(_delete(env, alice, entry))
        )
        assert sorted(outcomes, key=str) == [404, "ok"]
    assert (await _counts(env, alice))[:2] == (0, 5)


async def test_purchased_racing_delete_never_writes_a_trashed_row(env):
    """Either the DELETE commits first (purchased then adds a new live
    entry and the trashed row is untouched) or purchased promotes the live
    entry first (and the DELETE then trashes it). Never a 500, never a
    trashed row changed after it was trashed."""
    import asyncio

    for _ in range(6):
        alice = await env.make_user()
        book = await env.make_book()
        entry = await _entry(env, alice, book)

        async def purchase(user=alice, book_id=book.id):
            async with env.factory() as db:
                return await user_books_api.add_purchased.__wrapped__(
                    request=_req(),
                    req=PurchasedCreate(book_id=book_id),
                    current_user=user,
                    db=db,
                )

        outcomes = await asyncio.gather(
            _outcome(_delete(env, alice, entry)), _outcome(purchase())
        )
        assert outcomes == ["ok", "ok"]
        async with env.factory() as db:
            rows = (
                await db.scalars(select(UserBook).where(UserBook.user_id == alice.id))
            ).all()
        trashed = [r for r in rows if r.deleted_at is not None]
        live = [r for r in rows if r.deleted_at is None]
        assert [r.id for r in trashed] == [entry]
        if live:  # DELETE first: a new entry, the trashed one untouched
            assert [r.status for r in live] == ["purchased"]
            assert trashed[0].status == "wishlisted"
            assert trashed[0].purchased_at is None
        else:  # purchased first: promoted, then trashed
            assert trashed[0].status == "purchased"


async def test_restore_on_a_merged_row_racing_a_survivor_add_leaves_one_entry(env):
    """C5: the unique index is per book row, so a restore of an entry on a
    merged row and an add of its survivor could both pass their checks.
    Both take the user lock first: exactly one live entry, the other 409."""
    import asyncio

    survivor = await env.make_book()
    merged = await env.make_book(
        title=f"Dune duplicate {env.tag}", merged_into_id=survivor.id
    )
    for _ in range(6):
        alice = await env.make_user()
        on_merged = await _entry(env, alice, merged)
        await _delete(env, alice, on_merged)
        outcomes = await asyncio.gather(
            _outcome(_restore(env, alice, on_merged)),
            _outcome(_wishlist(env, alice, survivor)),
        )
        assert sorted(outcomes, key=str) == [409, "ok"]
        live, _, _ = await _counts(env, alice)
        assert live == 1

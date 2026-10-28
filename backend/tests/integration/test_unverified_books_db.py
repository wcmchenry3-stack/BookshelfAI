"""Unverified entries on real Postgres (#616).

Through the real endpoints and services: a user adds a book the catalog
cannot find as their own unverified entry; it is listed (``unverified:
true``), updatable and removable by them only, never visible to or matched
by another user, and never touches ``books`` / ``editions``, so a real
catalog book with the same ISBN, added afterwards by another user (or the
same one), is unaffected. Also: duplicates and the per-user cap, the table
constraints, and migration 0016 down and up.

Needs DATABASE_URL pointing at a Postgres database.
"""

import asyncio
import importlib.util
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from sqlalchemy import delete, func, insert, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import user_books as user_books_api
from app.core import catalog_token
from app.core.config import settings
from app.core.database import Base
from app.models.book import Book
from app.models.edition import Edition
from app.models.user import User
from app.models.user_book import UserBook
from app.models.user_custom_book import UserCustomBook
from app.schemas.book import EditionPreview, EnrichedBook
from app.schemas.user_book import (
    UnverifiedBookCreate,
    UserBookRead,
    UserBookUpdate,
    WishlistRequest,
)
from app.services.deduplication import DeduplicationService

pytestmark = pytest.mark.integration

# A valid ISBN-13 (check digit 7) and its ISBN-10 form.
ISBN_13 = "9780306406157"
ISBN_10 = "0306406152"


def _req():
    return SimpleNamespace()


@pytest.fixture
async def env():
    engine = create_async_engine(settings.async_database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    users: list[uuid.UUID] = []
    work = f"OL{uuid.uuid4().hex[:10]}W"

    async def make_user() -> User:
        uid = uuid.uuid4()
        async with factory() as db:
            db.add(User(id=uid, email=f"{uid}@example.com", google_id=f"g-{uid}"))
            await db.commit()
        users.append(uid)
        return User(id=uid, email=f"{uid}@example.com")

    yield factory, make_user, work

    async with factory() as db:
        await db.execute(delete(UserBook).where(UserBook.user_id.in_(users)))
        await db.execute(
            delete(UserCustomBook).where(UserCustomBook.user_id.in_(users))
        )
        book_ids = select(Book.id).where(Book.open_library_work_id == work)
        await db.execute(delete(Edition).where(Edition.book_id.in_(book_ids)))
        await db.execute(delete(Edition).where(Edition.isbn_13 == ISBN_13))
        await db.execute(delete(Book).where(Book.open_library_work_id == work))
        await db.execute(delete(User).where(User.id.in_(users)))
        await db.commit()
    await engine.dispose()


async def _add(factory, user, **fields):
    body = {"title": "Pond Notes", "author": "A. Local", **fields}
    async with factory() as db:
        ub = await user_books_api.add_unverified_to_wishlist.__wrapped__(
            request=_req(),
            req=UnverifiedBookCreate(**body),
            current_user=user,
            db=db,
        )
        return UserBookRead.model_validate(ub)


async def _list(factory, user):
    async with factory() as db:
        rows = await user_books_api.list_user_books.__wrapped__(
            request=_req(), status_filter=None, current_user=user, db=db
        )
        return [UserBookRead.model_validate(r) for r in rows]


async def _catalog_counts(factory):
    async with factory() as db:
        books = await db.scalar(select(func.count()).select_from(Book))
        editions = await db.scalar(select(func.count()).select_from(Edition))
        isbn = await db.scalar(
            select(func.count()).select_from(Edition).where(Edition.isbn_13 == ISBN_13)
        )
        return books, editions, isbn


async def test_create_and_list_for_owner_only(env):
    factory, make_user, _ = env
    alice, bob = await make_user(), await make_user()
    before = await _catalog_counts(factory)

    created = await _add(factory, alice, isbn=ISBN_10)

    assert created.unverified is True
    assert created.unverified_isbn_13 == ISBN_13
    assert created.status == "wishlisted"
    assert created.book.title == "Pond Notes"
    assert created.book.open_library_work_id is None
    assert created.book.google_books_id is None
    assert created.book.cover_url is None
    assert created.book.editions == []
    assert created.edition is None
    # Nothing in the shared catalog: no book, no edition, no ISBN claimed.
    assert await _catalog_counts(factory) == before

    listed = await _list(factory, alice)
    assert [(b.id, b.unverified) for b in listed] == [(created.id, True)]
    assert await _list(factory, bob) == []


async def test_other_user_cannot_update_or_delete(env):
    factory, make_user, _ = env
    alice, bob = await make_user(), await make_user()
    created = await _add(factory, alice)

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await user_books_api.update_user_book.__wrapped__(
                request=_req(),
                user_book_id=created.id,
                req=UserBookUpdate(status="read"),
                current_user=bob,
                db=db,
            )
        assert exc.value.status_code == 404
    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await user_books_api.delete_user_book.__wrapped__(
                request=_req(), user_book_id=created.id, current_user=bob, db=db
            )
        assert exc.value.status_code == 404
    # /purchased only takes catalog books: the entry's id is not one.
    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await user_books_api.add_purchased.__wrapped__(
                request=_req(),
                req=user_books_api.PurchasedCreate(book_id=created.book.id),
                current_user=bob,
                db=db,
            )
        assert exc.value.status_code == 404


async def test_owner_updates_then_trashes_both_rows(env):
    factory, make_user, _ = env
    alice = await make_user()
    created = await _add(factory, alice)

    async with factory() as db:
        updated = await user_books_api.update_user_book.__wrapped__(
            request=_req(),
            user_book_id=created.id,
            req=UserBookUpdate(status="read", rating=4),
            current_user=alice,
            db=db,
        )
        out = UserBookRead.model_validate(updated)
    assert (out.status, out.rating, out.unverified) == ("read", 4, True)

    async with factory() as db:
        await user_books_api.delete_user_book.__wrapped__(
            request=_req(), user_book_id=created.id, current_user=alice, db=db
        )
    # #683 replaces #616's hard delete: both rows go to the trash together.
    async with factory() as db:
        entry = await db.get(UserBook, created.id)
        custom = await db.get(UserCustomBook, created.book.id)
        assert entry.deleted_at is not None
        assert custom.deleted_at == entry.deleted_at
    assert await _list(factory, alice) == []


async def test_duplicate_and_isbn_duplicate_refused(env):
    factory, make_user, _ = env
    alice, bob = await make_user(), await make_user()
    await _add(factory, alice, isbn=ISBN_13)

    for fields in (
        {"title": "  pond   NOTES!", "author": "a local"},
        {"title": "Something else", "isbn": "0-306-40615-2"},
    ):
        with pytest.raises(HTTPException) as exc:
            await _add(factory, alice, **fields)
        assert exc.value.status_code == 409
        assert exc.value.detail == "unverified_duplicate"

    # Another user may hold the same title and ISBN: entries are private.
    other = await _add(factory, bob, isbn=ISBN_13)
    assert other.unverified_isbn_13 == ISBN_13


async def test_non_ascii_digits_are_stored_ascii_and_match_duplicates(env):
    factory, make_user, _ = env
    alice = await make_user()
    arabic_indic = "٩٧٨٠٣٠٦٤٠٦١٥٧"  # 9780306406157
    first = await _add(factory, alice, title="One", isbn=arabic_indic)
    assert first.unverified_isbn_13 == ISBN_13
    async with factory() as db:
        stored = await db.scalar(
            select(UserCustomBook.isbn_13).where(UserCustomBook.user_id == alice.id)
        )
    assert stored == ISBN_13 and stored.isascii()

    # The ASCII form of the same ISBN is the same book.
    with pytest.raises(HTTPException) as exc:
        await _add(factory, alice, title="Two", isbn=ISBN_13)
    assert exc.value.detail == "unverified_duplicate"


async def test_punctuation_only_titles_are_not_duplicates_of_each_other(env):
    factory, make_user, _ = env
    alice = await make_user()
    await _add(factory, alice, title="!!!", author="A. Local")
    # A different punctuation-only title is a different book...
    other = await _add(factory, alice, title="???", author="A. Local")
    assert other.book.title == "???"
    await _add(factory, alice, title="...", author="A. Local")
    # ...but the same one again is a duplicate.
    with pytest.raises(HTTPException) as exc:
        await _add(factory, alice, title=" !!! ", author="A. Local")
    assert exc.value.detail == "unverified_duplicate"
    # Likewise for an author of punctuation only.
    await _add(factory, alice, title="Same", author="—")
    await _add(factory, alice, title="Same", author="...")


async def test_cap_is_enforced_and_freed_by_delete(env):
    factory, make_user, _ = env
    alice = await make_user()
    with patch.object(settings, "unverified_books_per_user_max", 2):
        first = await _add(factory, alice, title="One")
        await _add(factory, alice, title="Two")
        with pytest.raises(HTTPException) as exc:
            await _add(factory, alice, title="Three")
        assert exc.value.status_code == 409
        assert exc.value.detail == "unverified_limit_reached"

        async with factory() as db:
            await user_books_api.delete_user_book.__wrapped__(
                request=_req(), user_book_id=first.id, current_user=alice, db=db
            )
        await _add(factory, alice, title="Three")


async def test_concurrent_adds_cannot_exceed_cap(env):
    factory, make_user, _ = env
    alice = await make_user()
    with patch.object(settings, "unverified_books_per_user_max", 3):
        outcomes = await asyncio.gather(
            *(_add(factory, alice, title=f"Book {i}") for i in range(6)),
            return_exceptions=True,
        )
    ok = [o for o in outcomes if isinstance(o, UserBookRead)]
    refused = [o for o in outcomes if isinstance(o, HTTPException)]
    assert len(ok) == 3
    assert len(refused) == 3
    assert {r.detail for r in refused} == {"unverified_limit_reached"}
    async with factory() as db:
        count = await db.scalar(
            select(func.count())
            .select_from(UserCustomBook)
            .where(UserCustomBook.user_id == alice.id)
        )
    assert count == 3


async def test_real_catalog_book_with_same_isbn_is_unaffected(env):
    """An unverified entry holding an ISBN never squats it: another user
    (and the creator too) can still add the real catalog book with that
    ISBN, which creates its edition, and the creator's entry does not make
    it "already owned"."""
    factory, make_user, work = env
    alice, bob = await make_user(), await make_user()
    await _add(factory, alice, title="Dune", author="Frank Herbert", isbn=ISBN_13)

    req = WishlistRequest(
        open_library_work_id=work,
        title="Dune",
        author="Frank Herbert",
        editions=[EditionPreview(isbn_13=ISBN_13)],
    )
    for user in (bob, alice):
        signed = req.model_copy(
            update={"catalog_token": catalog_token.sign(req, user.id)}
        )
        # The library check sees only catalog books: not owned for either.
        async with factory() as db:
            [marked] = await DeduplicationService().check(
                db,
                user.id,
                [
                    EnrichedBook(
                        open_library_work_id=work,
                        title="Dune",
                        author="Frank Herbert",
                        confidence=0.9,
                        editions=[EditionPreview(isbn_13=ISBN_13)],
                    )
                ],
            )
        assert marked.ownership == "not_owned"
        async with factory() as db:
            added = await user_books_api.add_to_wishlist.__wrapped__(
                request=_req(), req=signed, current_user=user, db=db
            )
            out = UserBookRead.model_validate(added)
        assert out.unverified is False
        assert out.edition is not None and out.edition.isbn_13 == ISBN_13

    async with factory() as db:
        edition = await db.scalar(select(Edition).where(Edition.isbn_13 == ISBN_13))
        book = await db.get(Book, edition.book_id)
    assert book.open_library_work_id == work
    # Alice now has both: her unverified entry and the catalog book.
    assert sorted(b.unverified for b in await _list(factory, alice)) == [False, True]


async def test_unverified_entry_after_catalog_book_with_same_isbn(env):
    """The reverse order: Bob's catalog edition exists; Alice's unverified
    entry with that ISBN is stored on her row only and leaves it alone."""
    factory, make_user, work = env
    alice, bob = await make_user(), await make_user()
    req = WishlistRequest(
        open_library_work_id=work,
        title="Dune",
        author="Frank Herbert",
        editions=[EditionPreview(isbn_13=ISBN_13)],
    )
    req = req.model_copy(update={"catalog_token": catalog_token.sign(req, bob.id)})
    async with factory() as db:
        await user_books_api.add_to_wishlist.__wrapped__(
            request=_req(), req=req, current_user=bob, db=db
        )
    before = await _catalog_counts(factory)

    created = await _add(factory, alice, isbn=ISBN_13)

    assert created.unverified_isbn_13 == ISBN_13
    assert await _catalog_counts(factory) == before
    [bobs] = await _list(factory, bob)
    assert bobs.unverified is False and bobs.book.title == "Dune"


async def test_table_constraints(env):
    factory, make_user, _ = env
    alice = await make_user()
    async with factory() as db:
        custom = UserCustomBook(user_id=alice.id, title="T", author="A")
        db.add(custom)
        await db.flush()
        # Neither a catalog book nor an unverified entry.
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                db.add(UserBook(user_id=alice.id, status="wishlisted"))
                await db.flush()
        # Over-long title, wrong-length ISBN.
        for bad in (
            UserCustomBook(user_id=alice.id, title="x" * 501, author="A"),
            UserCustomBook(user_id=alice.id, title="T", author="A", isbn_13="123"),
        ):
            with pytest.raises(IntegrityError):
                async with db.begin_nested():
                    db.add(bad)
                    await db.flush()
        await db.rollback()


async def test_account_deletion_removes_entries(env):
    factory, make_user, _ = env
    alice = await make_user()
    created = await _add(factory, alice)
    async with factory() as db:
        await db.execute(delete(User).where(User.id == alice.id))
        await db.commit()
    async with factory() as db:
        assert await db.get(UserBook, created.id) is None
        assert await db.get(UserCustomBook, created.book.id) is None


# ── Migration 0016 ───────────────────────────────────────────────────────────

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0016_user_custom_books.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0016", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_cycle(connection) -> None:
    """Downgrade with a catalog entry and an unverified entry present, then
    upgrade again: the downgrade removes the unverified entry only and
    restores ``book_id NOT NULL``; the upgrade brings the table back."""
    migration = _load_migration()
    uid, book_id, custom_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    connection.execute(
        insert(User).values(id=uid, email=f"{uid}@example.com", google_id=f"g-{uid}")
    )
    connection.execute(insert(Book).values(id=book_id, title="Dune", author="FH"))
    connection.execute(
        insert(UserCustomBook).values(id=custom_id, user_id=uid, title="T", author="A")
    )
    catalog_entry = connection.execute(
        insert(UserBook)
        .values(user_id=uid, book_id=book_id, status="wishlisted")
        .returning(UserBook.id)
    ).scalar_one()
    connection.execute(
        insert(UserBook).values(
            user_id=uid, custom_book_id=custom_id, status="wishlisted"
        )
    )

    context = MigrationContext.configure(connection)
    with Operations.context(context):
        migration.downgrade()
        insp = inspect(connection)
        assert "user_custom_books" not in insp.get_table_names()
        columns = {c["name"]: c for c in insp.get_columns("user_books")}
        assert "custom_book_id" not in columns
        assert columns["book_id"]["nullable"] is False
        remaining = (
            connection.execute(select(UserBook.id).where(UserBook.user_id == uid))
            .scalars()
            .all()
        )
        assert remaining == [catalog_entry]

        migration.upgrade()
        insp = inspect(connection)
        assert "user_custom_books" in insp.get_table_names()
        columns = {c["name"]: c for c in insp.get_columns("user_books")}
        assert columns["book_id"]["nullable"] is True
        assert "custom_book_id" in columns
        checks = {c["name"] for c in insp.get_check_constraints("user_books")}
        assert "ck_user_books_one_book" in checks
        connection.execute(
            insert(UserCustomBook).values(
                id=custom_id, user_id=uid, title="T", author="A"
            )
        )
        connection.execute(
            insert(UserBook).values(
                user_id=uid, custom_book_id=custom_id, status="wishlisted"
            )
        )


async def test_migration_0016_downgrade_and_upgrade():
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

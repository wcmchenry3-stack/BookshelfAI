"""Catalog/user split on real Postgres (#682).

- Migration 0024 down and up: the new columns, constraints and index go and
  come back with no difference to the models, and the upgrade backfills
  ``books.source`` / ``fetched_at`` for rows that existed before it. Runs in
  a rolled-back transaction.
- Overrides through the real PATCH /user-books endpoint: the user's own
  library read shows them, ``books`` is unchanged and another user's read of
  the same book still shows the catalog values; a null resets one. The table
  constraints refuse overrides on an unverified entry and over-long values.
- ``merged_into_id``: a library read follows it to the survivor; POST
  /purchased and POST /wishlist redirect a merged row to the survivor (no
  new entry targets a merged row) and promote an entry on a merged row
  rather than add a second one; the edition check runs against the
  survivor; the library check (scan, search and POST /wishlist alike)
  counts an entry on any row of a merged work for any of the work's IDs; a
  survivor cannot be deleted while a row is merged into it (ON DELETE
  RESTRICT).
- IDOR: another user's PATCH of an entry is a 404 and changes nothing.
- A catalog row created through POST /wishlist records its provenance.

Needs DATABASE_URL pointing at a Postgres database.
"""

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from sqlalchemy import delete, inspect, or_, select, update
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
    PurchasedCreate,
    UnverifiedBookCreate,
    UserBookRead,
    UserBookUpdate,
    WishlistRequest,
)
from app.services.deduplication import DeduplicationService, held_entries

pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0024_catalog_user_split.py"
)


def _req():
    return SimpleNamespace()


# ── Migration ────────────────────────────────────────────────────────────────


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0024", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_NEW_BOOK_COLUMNS = {"source", "fetched_at", "merged_into_id"}
_NEW_USER_BOOK_COLUMNS = {"title_override", "author_override", "cover_url_override"}


def _migration_cycle(connection) -> None:
    migration = _load_migration()
    assert (migration.revision, migration.down_revision) == ("0024", "0023")
    context = MigrationContext.configure(connection)

    def columns(table) -> set:
        return {c["name"] for c in inspect(connection).get_columns(table)}

    def diffs() -> list:
        names = _NEW_BOOK_COLUMNS | _NEW_USER_BOOK_COLUMNS
        return [
            d
            for d in compare_metadata(context, Base.metadata)
            if any(n in repr(d) for n in names)
        ]

    with Operations.context(context):
        assert _NEW_BOOK_COLUMNS <= columns("books")
        assert diffs() == []

        migration.downgrade()
        assert not (_NEW_BOOK_COLUMNS & columns("books"))
        assert not (_NEW_USER_BOOK_COLUMNS & columns("user_books"))

        # Books that exist before the upgrade, one per provenance rule.
        tag = uuid.uuid4().hex[:10]
        created = datetime(2025, 3, 1, tzinfo=UTC)
        rows = {
            "both": (f"gb{tag}", f"OL{tag}AW", "google_books"),
            "volume": (f"gc{tag}", None, "google_books"),
            "work": (None, f"OL{tag}BW", "open_library"),
            "neither": (None, None, "scan"),
        }
        ids = {}
        for i, (key, (volume, work, _)) in enumerate(rows.items()):
            ids[key] = uuid.uuid4()
            connection.execute(
                sa.text(
                    "INSERT INTO books (id, title, author, language, "
                    "google_books_id, open_library_work_id, created_at, updated_at) "
                    "VALUES (:id, 'T', 'A', 'en', :gb, :ol, :c, now())"
                ),
                {
                    "id": ids[key],
                    "gb": volume,
                    "ol": work,
                    "c": created + timedelta(days=i),
                },
            )

        migration.upgrade()
        assert _NEW_BOOK_COLUMNS <= columns("books")
        assert _NEW_USER_BOOK_COLUMNS <= columns("user_books")
        assert diffs() == []
        indexes = {i["name"] for i in inspect(connection).get_indexes("books")}
        assert "ix_books_merged_into_id" in indexes
        fks = inspect(connection).get_foreign_keys("books")
        (fk,) = [f for f in fks if f["constrained_columns"] == ["merged_into_id"]]
        assert fk["referred_table"] == "books"
        assert fk["options"].get("ondelete") == "RESTRICT"
        for i, (key, (_, _, source)) in enumerate(rows.items()):
            got = connection.execute(
                sa.text("SELECT source, fetched_at FROM books WHERE id = :id"),
                {"id": ids[key]},
            ).one()
            assert got.source == source, key
            assert got.fetched_at == created + timedelta(days=i), key
        # No book is left without provenance: a second backfill has nothing
        # to do.
        assert migration.backfill_provenance(connection) == 0
        assert (
            connection.execute(
                sa.text("SELECT count(*) FROM books WHERE source IS NULL")
            ).scalar_one()
            == 0
        )


async def test_migration_0024_upgrade_downgrade_cycle():
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


# ── Endpoints ────────────────────────────────────────────────────────────────


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
        data = {"title": "Dune", "author": "Frank Herbert", **fields}
        async with factory() as db:
            book = Book(**data)
            db.add(book)
            await db.commit()
            return book

    yield SimpleNamespace(
        factory=factory, make_user=make_user, make_book=make_book, tag=tag
    )

    async with factory() as db:
        await db.execute(delete(UserBook).where(UserBook.user_id.in_(users)))
        await db.execute(
            delete(UserCustomBook).where(UserCustomBook.user_id.in_(users))
        )
        ours = Book.title.like(f"%{tag}%") | Book.open_library_work_id.like(f"%{tag}%")
        book_ids = select(Book.id).where(ours)
        await db.execute(delete(Edition).where(Edition.book_id.in_(book_ids)))
        # Merged rows first (they reference their survivor).
        await db.execute(delete(Book).where(ours, Book.merged_into_id.isnot(None)))
        await db.execute(delete(Book).where(ours))
        await db.execute(delete(User).where(User.id.in_(users)))
        await db.commit()
    await engine.dispose()


async def _list(env, user) -> list[UserBookRead]:
    async with env.factory() as db:
        rows = await user_books_api.list_user_books.__wrapped__(
            request=_req(), status_filter=None, current_user=user, db=db
        )
        return [UserBookRead.model_validate(r) for r in rows]


async def _patch(env, user, entry_id, **body) -> UserBookRead:
    async with env.factory() as db:
        row = await user_books_api.update_user_book.__wrapped__(
            request=_req(),
            user_book_id=entry_id,
            req=UserBookUpdate(**body),
            current_user=user,
            db=db,
        )
        return UserBookRead.model_validate(row)


async def _entry(env, user, book) -> uuid.UUID:
    async with env.factory() as db:
        ub = UserBook(user_id=user.id, book_id=book.id, status="wishlisted")
        db.add(ub)
        await db.commit()
        return ub.id


async def _book_row(env, book_id) -> Book:
    async with env.factory() as db:
        return await db.get(Book, book_id)


async def test_overrides_are_this_users_only_and_never_touch_books(env):
    alice, bob = await env.make_user(), await env.make_user()
    book = await env.make_book(
        title=f"Dune {env.tag}", cover_url="https://books.example/dune.jpg"
    )
    before = await _book_row(env, book.id)
    alice_entry = await _entry(env, alice, book)
    await _entry(env, bob, book)

    got = await _patch(
        env,
        alice,
        alice_entry,
        title_override="Dune (my copy)",
        author_override="F. Herbert",
    )
    assert got.book.title == "Dune (my copy)"
    assert got.book.author == "F. Herbert"
    assert got.book.cover_url == "https://books.example/dune.jpg"
    assert got.book.id == book.id
    assert got.overrides.title == "Dune (my copy)"
    assert got.overrides.cover_url is None

    (listed,) = await _list(env, alice)
    assert listed.book.title == "Dune (my copy)"

    # The shared row and Bob's view are unchanged.
    after = await _book_row(env, book.id)
    assert (after.title, after.author, after.cover_url, after.updated_at) == (
        before.title,
        before.author,
        before.cover_url,
        before.updated_at,
    )
    (bobs,) = await _list(env, bob)
    assert bobs.book.title == f"Dune {env.tag}"
    assert bobs.book.author == "Frank Herbert"
    assert bobs.overrides.title is None

    # A null resets one override; a PATCH without it leaves it alone.
    got = await _patch(env, alice, alice_entry, title_override=None, rating=4)
    assert got.book.title == f"Dune {env.tag}"
    assert got.overrides.title is None
    assert got.book.author == "F. Herbert"
    assert got.rating == 4


async def test_unverified_entry_takes_no_overrides(env):
    alice = await env.make_user()
    async with env.factory() as db:
        created = await user_books_api.add_unverified_to_wishlist.__wrapped__(
            request=_req(),
            req=UnverifiedBookCreate(title=f"Pond {env.tag}", author="A"),
            current_user=alice,
            db=db,
        )
    with pytest.raises(HTTPException) as exc:
        await _patch(env, alice, created.id, title_override="X")
    assert exc.value.status_code == 422
    assert exc.value.detail == "overrides_not_supported"
    (listed,) = await _list(env, alice)
    assert listed.book.title == f"Pond {env.tag}"

    # The table refuses it too.
    async with env.factory() as db:
        with pytest.raises(IntegrityError):
            await db.execute(
                update(UserBook)
                .where(UserBook.id == created.id)
                .values(cover_url_override="https://x.example/c.jpg")
            )


async def test_override_length_is_bounded_by_the_table(env):
    alice = await env.make_user()
    book = await env.make_book(title=f"Dune {env.tag}")
    entry = await _entry(env, alice, book)
    async with env.factory() as db:
        with pytest.raises(IntegrityError):
            await db.execute(
                update(UserBook)
                .where(UserBook.id == entry)
                .values(title_override="x" * 501)
            )
    async with env.factory() as db:
        with pytest.raises(IntegrityError):
            await db.execute(
                update(UserBook).where(UserBook.id == entry).values(author_override="")
            )


async def _merged_pair(env):
    survivor = await env.make_book(
        title=f"Dune {env.tag}", open_library_work_id=f"OL{env.tag}SW"
    )
    merged = await env.make_book(
        title=f"Dune duplicate {env.tag}",
        open_library_work_id=f"OL{env.tag}MW",
        merged_into_id=survivor.id,
    )
    return survivor, merged


async def test_library_read_follows_merged_into_id(env):
    alice = await env.make_user()
    survivor, merged = await _merged_pair(env)
    async with env.factory() as db:
        db.add(Edition(book_id=survivor.id, isbn_13="979" + str(uuid.uuid4().int)[:10]))
        await db.commit()
    entry = await _entry(env, alice, merged)

    (listed,) = await _list(env, alice)
    assert listed.id == entry
    assert listed.book.id == survivor.id
    assert listed.book.title == f"Dune {env.tag}"
    assert len(listed.book.editions) == 1

    # Overrides still win over the survivor's values.
    got = await _patch(env, alice, entry, title_override="Mine")
    assert got.book.id == survivor.id
    assert got.book.title == "Mine"


async def test_purchased_redirects_a_merged_book_to_its_survivor(env):
    alice = await env.make_user()
    survivor, merged = await _merged_pair(env)
    async with env.factory() as db:
        row = await user_books_api.add_purchased.__wrapped__(
            request=_req(),
            req=PurchasedCreate(book_id=merged.id),
            current_user=alice,
            db=db,
        )
        assert row.book_id == survivor.id
    async with env.factory() as db:
        targets = (
            await db.scalars(
                select(UserBook.book_id).where(UserBook.user_id == alice.id)
            )
        ).all()
    assert targets == [survivor.id]


async def test_wishlist_redirects_a_merged_book_to_its_survivor(env):
    alice = await env.make_user()
    survivor, merged = await _merged_pair(env)
    # The merged row's work ID, posted without a token: links an existing
    # row, which is the survivor.
    async with env.factory() as db:
        row = await user_books_api.add_to_wishlist.__wrapped__(
            request=_req(),
            req=WishlistRequest(
                open_library_work_id=merged.open_library_work_id,
                title="Dune",
                author="Frank Herbert",
            ),
            current_user=alice,
            db=db,
        )
        assert row.book_id == survivor.id


async def test_library_check_counts_an_entry_on_a_merged_row(env):
    alice = await env.make_user()
    survivor, merged = await _merged_pair(env)
    await _entry(env, alice, merged)
    async with env.factory() as db:
        by_book = await held_entries(db, alice.id, book_ids={survivor.id})
        by_work = await held_entries(
            db, alice.id, work_ids={survivor.open_library_work_id}
        )
    assert [h.book_id for h in by_book] == [survivor.id]
    assert [h.book_id for h in by_work] == [survivor.id]
    # The entry keeps its own book's IDs and carries the whole work's.
    assert by_work[0].open_library_work_id == merged.open_library_work_id
    assert by_work[0].work_ids == {
        survivor.open_library_work_id,
        merged.open_library_work_id,
    }
    assert by_work[0].book_ids == {survivor.id, merged.id}


async def _wishlist_by_work(env, user, work_id):
    async with env.factory() as db:
        return await user_books_api.add_to_wishlist.__wrapped__(
            request=_req(),
            req=WishlistRequest(
                open_library_work_id=work_id, title="Dune", author="Frank Herbert"
            ),
            current_user=user,
            db=db,
        )


async def _scan_ownership(env, user, **ids) -> str:
    candidate = EnrichedBook(
        title="Dune", author="Frank Herbert", confidence=0.9, editions=[], **ids
    )
    async with env.factory() as db:
        (checked,) = await DeduplicationService().check(db, user.id, [candidate])
    return checked.ownership


@pytest.mark.parametrize("entry_on", ["survivor", "merged"])
@pytest.mark.parametrize("candidate_of", ["survivor", "merged"])
async def test_scan_and_wishlist_agree_on_any_id_of_a_merged_work(
    env, entry_on, candidate_of
):
    """#587 for a merged work (#682): a scan/search candidate carrying the
    survivor's or the merged row's work or volume ID is owned whichever row
    the entry is on, and POST /wishlist refuses it with the same answer."""
    alice = await env.make_user()
    survivor = await env.make_book(
        title=f"Dune {env.tag}",
        open_library_work_id=f"OL{env.tag}SW",
        google_books_id=f"gs{env.tag}",
    )
    merged = await env.make_book(
        title=f"Dune duplicate {env.tag}",
        open_library_work_id=f"OL{env.tag}MW",
        google_books_id=f"gm{env.tag}",
        merged_into_id=survivor.id,
    )
    rows = {"survivor": survivor, "merged": merged}
    await _entry(env, alice, rows[entry_on])
    candidate = rows[candidate_of]

    assert (
        await _scan_ownership(
            env, alice, open_library_work_id=candidate.open_library_work_id
        )
        == "owned_edition_unknown"
    )
    assert (
        await _scan_ownership(env, alice, google_books_id=candidate.google_books_id)
        == "owned_edition_unknown"
    )
    with pytest.raises(HTTPException) as exc:
        await _wishlist_by_work(env, alice, candidate.open_library_work_id)
    assert (exc.value.status_code, exc.value.detail) == (409, "owned_edition_unknown")
    async with env.factory() as db:
        count = (
            await db.scalars(select(UserBook.id).where(UserBook.user_id == alice.id))
        ).all()
    assert len(count) == 1


async def test_another_users_merged_work_is_not_owned(env):
    alice, bob = await env.make_user(), await env.make_user()
    survivor, merged = await _merged_pair(env)
    await _entry(env, bob, merged)
    for work in (survivor.open_library_work_id, merged.open_library_work_id):
        assert (
            await _scan_ownership(env, alice, open_library_work_id=work) == "not_owned"
        )
        # Bob's entry on the merged row does count for Bob.
        assert await _scan_ownership(env, bob, open_library_work_id=work) != "not_owned"


async def _purchase(env, user, **body):
    async with env.factory() as db:
        return await user_books_api.add_purchased.__wrapped__(
            request=_req(),
            req=PurchasedCreate(**body),
            current_user=user,
            db=db,
        )


async def _entries(env, user) -> list[UserBook]:
    async with env.factory() as db:
        return list(
            await db.scalars(select(UserBook).where(UserBook.user_id == user.id))
        )


@pytest.mark.parametrize("posted", ["survivor", "merged"])
async def test_purchased_promotes_an_entry_on_a_merged_row(env, posted):
    """An entry on a merged row is an entry of the survivor's work: POST
    /purchased with either row's id promotes it, never adds a second one."""
    alice = await env.make_user()
    survivor, merged = await _merged_pair(env)
    entry = await _entry(env, alice, merged)
    target = survivor if posted == "survivor" else merged

    row = await _purchase(env, alice, book_id=target.id)

    assert row.id == entry
    assert row.status == "purchased"
    (only,) = await _entries(env, alice)
    assert (only.id, only.status) == (entry, "purchased")


async def test_purchased_prefers_the_survivors_own_entry(env):
    alice = await env.make_user()
    survivor, merged = await _merged_pair(env)
    await _entry(env, alice, merged)
    on_survivor = await _entry(env, alice, survivor)

    row = await _purchase(env, alice, book_id=merged.id)

    assert row.id == on_survivor
    assert len(await _entries(env, alice)) == 2


async def test_purchased_edition_check_uses_the_survivor(env):
    """#686 after a merge (#682): editions move to the survivor, so an
    edition is accepted only when it is the survivor's, whichever row's id
    is posted. An edition still on the merged row (not moved) is refused."""
    alice = await env.make_user()
    survivor, merged = await _merged_pair(env)
    async with env.factory() as db:
        of_survivor = Edition(
            book_id=survivor.id, isbn_13="979" + str(uuid.uuid4().int)[:10]
        )
        of_merged = Edition(
            book_id=merged.id, isbn_13="979" + str(uuid.uuid4().int)[:10]
        )
        db.add_all([of_survivor, of_merged])
        await db.commit()

    for posted in (merged, survivor):
        with pytest.raises(HTTPException) as exc:
            await _purchase(env, alice, book_id=posted.id, edition_id=of_merged.id)
        assert (exc.value.status_code, exc.value.detail) == (
            422,
            "edition_book_mismatch",
        )
    assert await _entries(env, alice) == []

    row = await _purchase(env, alice, book_id=merged.id, edition_id=of_survivor.id)
    assert (row.book_id, row.edition_id) == (survivor.id, of_survivor.id)


async def test_another_users_entry_cannot_be_patched(env):
    """IDOR: Bob's PATCH of Alice's entry is a 404 and changes nothing."""
    alice, bob = await env.make_user(), await env.make_user()
    book = await env.make_book(title=f"Dune {env.tag}")
    entry = await _entry(env, alice, book)
    await _patch(env, alice, entry, title_override="Alice's", author_override="A")

    with pytest.raises(HTTPException) as exc:
        await _patch(
            env,
            bob,
            entry,
            title_override="Bob's",
            author_override="B",
            cover_url_override=None,
            status="read",
        )
    assert exc.value.status_code == 404

    async with env.factory() as db:
        row = await db.get(UserBook, entry)
    assert (row.user_id, row.status) == (alice.id, "wishlisted")
    assert (row.title_override, row.author_override, row.cover_url_override) == (
        "Alice's",
        "A",
        None,
    )


async def test_survivor_cannot_be_deleted_while_rows_are_merged_into_it(env):
    survivor, _ = await _merged_pair(env)
    async with env.factory() as db:
        with pytest.raises(IntegrityError):
            await db.execute(delete(Book).where(Book.id == survivor.id))


async def test_a_row_cannot_be_merged_into_itself(env):
    book = await env.make_book(title=f"Dune {env.tag}")
    async with env.factory() as db:
        with pytest.raises(IntegrityError):
            await db.execute(
                update(Book).where(Book.id == book.id).values(merged_into_id=book.id)
            )


async def test_unknown_source_is_refused(env):
    book = await env.make_book(title=f"Dune {env.tag}")
    async with env.factory() as db:
        with pytest.raises(IntegrityError):
            await db.execute(
                update(Book).where(Book.id == book.id).values(source="client")
            )


async def test_wishlist_records_provenance_on_a_new_book(env):
    alice = await env.make_user()
    work = f"OL{env.tag}PW"
    book = EnrichedBook(
        open_library_work_id=work,
        google_books_id=None,
        title=f"Provenance {env.tag}",
        author="A",
        confidence=0.9,
        editions=[EditionPreview(isbn_13=None)],
    )
    (signed,) = catalog_token.sign_books([book], alice.id)
    body = signed.model_dump(
        exclude={"book_id", "confidence", "already_in_library", "in_library"}
    )
    started = datetime.now(UTC)
    async with env.factory() as db:
        await user_books_api.add_to_wishlist.__wrapped__(
            request=_req(),
            req=WishlistRequest(**body),
            current_user=alice,
            db=db,
        )
    async with env.factory() as db:
        row = (
            await db.scalars(
                select(Book).where(
                    or_(Book.open_library_work_id == work, Book.title == body["title"])
                )
            )
        ).one()
    assert row.source == "open_library"
    assert started <= row.fetched_at <= datetime.now(UTC)
    assert row.merged_into_id is None

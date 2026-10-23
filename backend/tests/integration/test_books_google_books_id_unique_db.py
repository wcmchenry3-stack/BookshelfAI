"""Migration 0015 against real PostgreSQL (#613): duplicate ``books`` rows
with the same ``google_books_id`` are merged into the oldest one (its
``user_books`` and ``editions`` re-pointed, conflicting entries merged and
deleted), then the partial unique index ``uq_books_google_books_id`` is
created; the downgrade drops the index only.

The cycle runs in a transaction that is rolled back (Postgres DDL is
transactional), so the shared test database is left as it was.

Skipped automatically when DATABASE_URL is the dummy URL (see conftest.py).
"""

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import insert, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.core.database import Base
from app.models.book import Book
from app.models.edition import Edition
from app.models.user import User
from app.models.user_book import UserBook

pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0015_books_google_books_id_unique.py"
)
INDEX = "uq_books_google_books_id"
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0015", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _index(connection) -> dict | None:
    for ix in inspect(connection).get_indexes("books"):
        if ix["name"] == INDEX:
            return ix
    return None


def _book(connection, volume, minutes, **extra) -> uuid.UUID:
    book_id = uuid.uuid4()
    connection.execute(
        insert(Book).values(
            id=book_id,
            google_books_id=volume,
            title="Dune",
            author="Frank Herbert",
            created_at=T0 + timedelta(minutes=minutes),
            **extra,
        )
    )
    return book_id


def _user(connection) -> uuid.UUID:
    uid = uuid.uuid4()
    connection.execute(
        insert(User).values(id=uid, email=f"{uid}@example.com", google_id=f"g-{uid}")
    )
    return uid


def _entry(connection, user_id, book_id, minutes, **extra) -> uuid.UUID:
    ub_id = uuid.uuid4()
    connection.execute(
        insert(UserBook).values(
            id=ub_id,
            user_id=user_id,
            book_id=book_id,
            status=extra.pop("status", "wishlisted"),
            created_at=T0 + timedelta(minutes=minutes),
            **extra,
        )
    )
    return ub_id


def _edition(connection, book_id, isbn) -> uuid.UUID:
    ed_id = uuid.uuid4()
    connection.execute(
        insert(Edition).values(
            id=ed_id, book_id=book_id, isbn_13=isbn, format="unknown"
        )
    )
    return ed_id


def _isbn() -> str:
    return "979" + str(uuid.uuid4().int)[:10]


def _cycle(connection) -> None:
    migration = _load_migration()
    assert (migration.revision, migration.down_revision) == ("0015", "0014")
    context = MigrationContext.configure(connection)
    tag = uuid.uuid4().hex[:10]
    vol, other_vol = f"gb{tag}", f"gbo{tag}"

    with Operations.context(context):
        if _index(connection) is not None:
            migration.downgrade()
        assert _index(connection) is None

        # Three rows for one volume (inserted newest first, so only
        # created_at decides which is kept), two for another; a unique one.
        newest = _book(connection, vol, 20)
        newer = _book(connection, vol, 10, open_library_work_id=f"OL{tag}W")
        oldest = _book(connection, vol, 0)
        o_new = _book(connection, other_vol, 5)
        o_old = _book(connection, other_vol, 1, open_library_work_id=f"OLo{tag}W")
        single = _book(connection, f"gbs{tag}", 0)
        no_volume = [_book(connection, None, 0), _book(connection, None, 1)]

        e_newer = _edition(connection, newer, _isbn())
        e_newest = _edition(connection, newest, _isbn())
        e_oldest = _edition(connection, oldest, _isbn())

        # Holds the kept row and both duplicates; the kept entry has empty
        # fields the duplicates' entries fill (oldest first).
        both = _user(connection)
        both_kept = _entry(connection, both, oldest, 30)
        _entry(
            connection,
            both,
            newer,
            10,
            status="read",
            edition_id=e_newer,
            notes="first notes",
            rating=4,
        )
        _entry(connection, both, newest, 20, notes="later notes", rating=2)
        # Holds only the newer duplicates: the oldest of their entries
        # survives, moved to the kept row, with the other's fields merged.
        only_dupes = _user(connection)
        dupes_kept = _entry(
            connection, only_dupes, newest, 5, status="reading", rating=5
        )
        _entry(connection, only_dupes, newer, 7, status="purchased", notes="mine")
        # Holds only the newest duplicate.
        newest_only = _user(connection)
        newest_only_entry = _entry(
            connection, newest_only, newest, 3, status="purchased", rating=3
        )
        # An entry on a non-duplicated book is untouched.
        single_entry = _entry(connection, newest_only, single, 3)

        totals = migration.merge_duplicates(connection)
        assert totals == {
            "books": 3,
            "user_books_deleted": 3,
            "user_books_moved": 2,
            "editions_moved": 2,
        }
        # The whole upgrade: its merge finds nothing more, then the index.
        migration.upgrade()
        ix = _index(connection)
        assert ix is not None and migration.INDEX == INDEX
        assert ix["unique"] and ix["column_names"] == ["google_books_id"]
        assert "google_books_id IS NOT NULL" in str(
            ix.get("dialect_options", {}).get("postgresql_where")
        )

        books = {
            r.id: r
            for r in connection.execute(
                select(Book.id, Book.open_library_work_id).where(
                    Book.google_books_id.in_([vol, other_vol, f"gbs{tag}"])
                )
            )
        }
        assert set(books) == {oldest, o_old, single}
        assert not {newer, newest, o_new} & set(books)
        # The kept row had no Open Library work ID: the duplicate's moved.
        assert books[oldest].open_library_work_id == f"OL{tag}W"
        assert books[o_old].open_library_work_id == f"OLo{tag}W"
        assert (
            len(connection.execute(select(Book.id).where(Book.id.in_(no_volume))).all())
            == 2
        )

        editions = dict(
            connection.execute(
                select(Edition.id, Edition.book_id).where(
                    Edition.id.in_([e_newer, e_newest, e_oldest])
                )
            ).all()
        )
        assert editions == {e_newer: oldest, e_newest: oldest, e_oldest: oldest}

        rows = connection.execute(
            select(UserBook.id, UserBook.user_id).where(
                UserBook.user_id.in_([both, only_dupes, newest_only])
            )
        ).all()
        assert len(rows) == 4  # one per user per book, plus single_entry
        b = connection.execute(select(UserBook).where(UserBook.user_id == both)).one()
        # The kept-row entry was wishlisted, a duplicate's read: the most
        # advanced status wins.
        assert (b.id, b.book_id, b.status) == (both_kept, oldest, "read")
        assert (b.edition_id, b.notes, b.rating) == (e_newer, "first notes", 4)
        d = connection.execute(
            select(UserBook).where(UserBook.user_id == only_dupes)
        ).one()
        assert (d.id, d.book_id, d.notes, d.rating) == (dupes_kept, oldest, "mine", 5)
        assert d.status == "reading"  # already the most advanced: kept
        n = connection.execute(
            select(UserBook).where(
                UserBook.user_id == newest_only, UserBook.book_id != single
            )
        ).one()
        assert (n.id, n.book_id, n.status, n.rating) == (
            newest_only_entry,
            oldest,
            "purchased",
            3,
        )
        assert (
            connection.execute(
                select(UserBook.book_id).where(UserBook.id == single_entry)
            ).scalar_one()
            == single
        )

        # The index now refuses a new duplicate, but not more NULLs.
        savepoint = connection.begin_nested()
        with pytest.raises(IntegrityError, match=INDEX):
            _book(connection, vol, 40)
        savepoint.rollback()
        _book(connection, None, 50)

        # Downgrade drops the index only; the merge is not undone.
        migration.downgrade()
        assert _index(connection) is None
        assert (
            len(
                connection.execute(
                    select(Book.id).where(Book.google_books_id == vol)
                ).all()
            )
            == 1
        )
        _book(connection, vol, 60)  # duplicates are possible again
        connection.execute(
            Book.__table__.delete().where(
                Book.google_books_id == vol,
                Book.created_at == T0 + timedelta(minutes=60),
            )
        )
        migration.upgrade()
        assert _index(connection) is not None


async def test_migration_merges_duplicates_and_cycles():
    engine = create_async_engine(settings.async_database_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with engine.connect() as conn:
            trans = await conn.begin()
            try:
                await conn.run_sync(_cycle)
            finally:
                await trans.rollback()
    finally:
        await engine.dispose()

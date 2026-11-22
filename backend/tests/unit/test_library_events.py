"""Unit tests for the library audit log helpers (#684).

The database is faked; the real flows (every endpoint, rollbacks, the
retention job) run on Postgres in ``tests/integration/test_library_events_db.py``.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import Settings, settings
from app.models.library_event import EVENT_TYPES, LibraryEvent
from app.models.user_book import UserBook
from app.services import library_events, library_events_retention

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
NOTES = "my private thoughts about this book"


def _db():
    db = MagicMock()
    db.flush = AsyncMock()
    db.execute = AsyncMock()
    return db


def _entry(**fields) -> UserBook:
    base = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "book_id": uuid.uuid4(),
        "status": "wishlisted",
    }
    return UserBook(**{**base, **fields})


def _added(db) -> list[LibraryEvent]:
    return [c.args[0] for c in db.add.call_args_list]


def test_every_tracked_field_is_a_user_books_column():
    columns = set(UserBook.__table__.columns.keys())
    assert set(library_events.TRACKED_FIELDS) <= columns
    assert library_events.FREE_TEXT_FIELDS <= set(library_events.TRACKED_FIELDS)


# user_books columns an event deliberately does not record (keys and
# bookkeeping timestamps). A new column must be added to TRACKED_FIELDS
# or here.
UNTRACKED_COLUMNS = frozenset({"id", "user_id", "created_at", "updated_at"})
# Text columns whose values are fixed codes, not something the user typed.
STRUCTURED_STRINGS = frozenset({"status"})


def _string_columns(columns) -> set[str]:
    return {c.name for c in columns if c.type.python_type is str}


def test_every_user_books_column_is_tracked_or_knowingly_untracked():
    columns = set(UserBook.__table__.columns.keys())
    tracked = set(library_events.TRACKED_FIELDS)
    assert columns - tracked - UNTRACKED_COLUMNS == set(), (
        "add the new user_books column to library_events.TRACKED_FIELDS "
        "(and FREE_TEXT_FIELDS if the user types it) or to UNTRACKED_COLUMNS"
    )


def test_every_user_set_text_column_is_masked():
    """Every String column of user_books, tracked or not, is masked free
    text, a known structured code or a known untracked column: a new one
    fails here until it is classified."""
    strings = _string_columns(UserBook.__table__.columns)
    unclassified = (
        strings
        - library_events.FREE_TEXT_FIELDS
        - STRUCTURED_STRINGS
        - UNTRACKED_COLUMNS
    )
    assert unclassified == set(), (
        f"classify {sorted(unclassified)}: add to library_events."
        "FREE_TEXT_FIELDS (recorded only as 'changed') unless it is a code"
    )
    assert library_events.FREE_TEXT_FIELDS <= strings


def test_the_text_column_check_catches_a_new_column():
    import sqlalchemy as sa

    columns = [*UserBook.__table__.columns, sa.Column("private_diary", sa.String)]
    unclassified = (
        _string_columns(columns)
        - library_events.FREE_TEXT_FIELDS
        - STRUCTURED_STRINGS
        - UNTRACKED_COLUMNS
    )
    assert unclassified == {"private_diary"}


def test_event_types_are_the_issue_list():
    assert set(EVENT_TYPES) == {
        "created",
        "updated",
        "status_changed",
        "deleted",
        "restored",
        "purged",
        "format_added",
        "format_removed",
    }


def test_changed_fields_masks_free_text_and_serialises_values():
    edition = uuid.uuid4()
    before = {"notes": None, "rating": None, "edition_id": None, "deleted_at": None}
    after = {
        "notes": NOTES,
        "rating": 4,
        "edition_id": edition,
        "deleted_at": NOW,
        "title_override": "My Title",
        "author_override": "Me",
        "cover_url_override": "https://example.com/c.jpg",
    }
    changes = library_events.changed_fields(before, after)
    assert changes == {
        "notes": "changed",
        "rating": {"old": None, "new": 4},
        "edition_id": {"old": None, "new": str(edition)},
        "deleted_at": {"old": None, "new": NOW.isoformat()},
        "title_override": "changed",
        "author_override": "changed",
        "cover_url_override": "changed",
    }
    flat = repr(changes)
    for text in (NOTES, "My Title", "example.com"):
        assert text not in flat


def test_changed_fields_ignores_unchanged_and_untracked_fields():
    same = {"status": "read", "rating": 3}
    assert library_events.changed_fields(same, dict(same)) == {}
    assert library_events.changed_fields({"title": "a"}, {"title": "b"}) == {}


async def test_record_created_lists_the_new_entrys_fields():
    db = _db()
    entry = _entry(status="purchased", purchased_at=NOW, notes=NOTES)
    with patch.object(library_events, "current_request_id", return_value="rid-1"):
        await library_events.record_created(db, entry)
    (event,) = _added(db)
    assert (event.user_id, event.user_book_id) == (entry.user_id, entry.id)
    assert event.event_type == "created"
    assert event.request_id == "rid-1"
    assert event.changed_fields["status"] == {"old": None, "new": "purchased"}
    assert event.changed_fields["book_id"] == {"old": None, "new": str(entry.book_id)}
    assert event.changed_fields["notes"] == "changed"
    assert NOTES not in repr(event.changed_fields)
    db.flush.assert_not_awaited()


async def test_record_created_flushes_a_new_entry_for_its_id():
    db = _db()
    entry = UserBook(user_id=uuid.uuid4(), status="wishlisted")

    async def flush():
        entry.id = uuid.uuid4()

    db.flush = AsyncMock(side_effect=flush)
    await library_events.record_created(db, entry)
    db.flush.assert_awaited_once()
    (event,) = _added(db)
    assert event.user_book_id == entry.id is not None


@pytest.mark.parametrize(
    "change, expected",
    [
        ({"status": "read"}, "status_changed"),
        ({"rating": 5}, "updated"),
        ({"notes": "x"}, "updated"),
    ],
)
async def test_record_change_picks_the_event_type(change, expected):
    db = _db()
    entry = _entry()
    before = library_events.snapshot(entry)
    for field, value in change.items():
        setattr(entry, field, value)
    assert await library_events.record_change(db, entry, before) is True
    (event,) = _added(db)
    assert event.event_type == expected


async def test_record_change_with_nothing_changed_writes_nothing():
    db = _db()
    entry = _entry(rating=3)
    before = library_events.snapshot(entry)
    entry.rating = 3
    assert await library_events.record_change(db, entry, before) is False
    db.add.assert_not_called()


async def test_record_change_takes_an_explicit_type():
    db = _db()
    entry = _entry()
    before = library_events.snapshot(entry)
    entry.deleted_at = NOW
    await library_events.record_change(db, entry, before, "deleted")
    (event,) = _added(db)
    assert event.event_type == "deleted"
    assert event.changed_fields == {"deleted_at": {"old": None, "new": NOW.isoformat()}}


async def test_record_purged_is_one_insert_for_all_rows():
    db = _db()
    rows = [(uuid.uuid4(), uuid.uuid4()), (uuid.uuid4(), uuid.uuid4())]
    await library_events.record_purged(db, rows)
    db.execute.assert_awaited_once()
    stmt, params = db.execute.await_args.args
    assert "library_events" in str(stmt)
    assert [(p["user_book_id"], p["user_id"]) for p in params] == rows
    assert {p["event_type"] for p in params} == {"purged"}
    assert all(p["changed_fields"] == {} for p in params)


async def test_record_purged_with_no_rows_issues_nothing():
    db = _db()
    await library_events.record_purged(db, [])
    db.execute.assert_not_awaited()


# ── Retention ────────────────────────────────────────────────────────────────


def test_retention_default_is_180_days_and_bounded():
    assert Settings.model_fields["library_events_retention_days"].default == 180
    assert library_events_retention.PURGE_BATCH_SIZE == 5_000
    assert library_events_retention.PURGE_MAX_BATCHES == 20


def test_retention_cutoff_follows_the_setting():
    with patch.object(settings, "library_events_retention_days", 7):
        cutoff = library_events_retention.retention_cutoff(NOW)
    assert (NOW - cutoff).days == 7


def _scalar(value):
    return SimpleNamespace(scalar_one=lambda: value)


def _ids(n):
    return SimpleNamespace(scalars=lambda: iter(range(n)))


async def test_retention_stops_when_the_lock_is_held():
    db = _db()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.execute = AsyncMock(side_effect=[_scalar(False)])
    assert await library_events_retention.purge_old_library_events(db, NOW) == 0
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


async def test_retention_batches_until_a_short_batch(monkeypatch):
    monkeypatch.setattr(library_events_retention, "PURGE_BATCH_SIZE", 2)
    db = _db()
    db.commit = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            _scalar(True),
            _ids(2),
            SimpleNamespace(rowcount=2),
            _scalar(True),
            _ids(1),
            SimpleNamespace(rowcount=1),
        ]
    )
    assert await library_events_retention.purge_old_library_events(db, NOW) == 3
    assert db.commit.await_count == 2


async def test_retention_with_nothing_old_deletes_nothing():
    db = _db()
    db.commit = AsyncMock()
    db.execute = AsyncMock(side_effect=[_scalar(True), _ids(0)])
    assert await library_events_retention.purge_old_library_events(db, NOW) == 0
    assert db.execute.await_count == 2
    db.commit.assert_awaited_once()


async def test_retention_stops_after_max_batches(monkeypatch):
    monkeypatch.setattr(library_events_retention, "PURGE_BATCH_SIZE", 1)
    monkeypatch.setattr(library_events_retention, "PURGE_MAX_BATCHES", 2)
    db = _db()
    db.commit = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[_scalar(True), _ids(1), SimpleNamespace(rowcount=1)] * 2
    )
    assert await library_events_retention.purge_old_library_events(db, NOW) == 2
    assert db.commit.await_count == 2

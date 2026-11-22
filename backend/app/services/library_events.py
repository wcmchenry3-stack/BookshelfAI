"""The library audit log (#684): one ``library_events`` row per change to a
library entry, written in the transaction that makes the change.

Every library mutation calls one of the helpers here before its commit and
never commits itself, so the event and the change commit or roll back
together:

- :func:`record_created` for a new entry (``POST /wishlist``,
  ``POST /wishlist/unverified``, a new entry from ``POST /purchased``);
- :func:`record_change` for a change to an existing one: ``PATCH``, the
  promotion in ``POST /purchased``, ``DELETE`` (``deleted``) and restore
  (``restored``). It compares a :func:`snapshot` taken before the change
  with the entry now and writes nothing when nothing changed;
- :func:`record_purged` for library rows hard-deleted by the trash
  (``app/services/trash.py``: the daily purge, same-book eviction and the
  per-user cap), one event per row, in one ``INSERT``.

``changed_fields`` maps each changed field to ``{"old": ..., "new": ...}``.
Free text (:data:`FREE_TEXT_FIELDS`: notes and the title / author / cover
overrides) is recorded as :data:`CHANGED` only, never its value. Values are
JSON: UUIDs as strings, timestamps in ISO 8601.

This module only inserts. Nothing in the app updates an event, and only
``app/services/library_events_retention.py`` deletes them
(``tests/security/test_library_events_append_only.py``).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.ledger import current_request_id
from app.models.library_event import (
    EVENT_CREATED,
    EVENT_PURGED,
    EVENT_STATUS_CHANGED,
    EVENT_UPDATED,
    LibraryEvent,
)
from app.models.user_book import UserBook

# The marker recorded in place of a free-text value.
CHANGED = "changed"

# The ``user_books`` columns an event records.
TRACKED_FIELDS = (
    "book_id",
    "custom_book_id",
    "edition_id",
    "status",
    "wishlisted_at",
    "purchased_at",
    "started_at",
    "finished_at",
    "rating",
    "notes",
    "title_override",
    "author_override",
    "cover_url_override",
    "deleted_at",
)
# Fields whose value the user typed: recorded as CHANGED, never copied.
FREE_TEXT_FIELDS = frozenset(
    {"notes", "title_override", "author_override", "cover_url_override"}
)

Snapshot = dict[str, Any]


def snapshot(entry: UserBook) -> Snapshot:
    """The tracked fields of ``entry`` as they are now (None for a field
    not set on it yet)."""
    return {field: getattr(entry, field, None) for field in TRACKED_FIELDS}


def _json(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def changed_fields(before: Snapshot, after: Snapshot) -> dict[str, Any]:
    """The fields that differ between two snapshots, free text masked."""
    changes: dict[str, Any] = {}
    for field in TRACKED_FIELDS:
        old, new = before.get(field), after.get(field)
        if old == new:
            continue
        if field in FREE_TEXT_FIELDS:
            changes[field] = CHANGED
        else:
            changes[field] = {"old": _json(old), "new": _json(new)}
    return changes


async def _add(
    db: AsyncSession,
    entry: UserBook,
    event_type: str,
    changes: dict[str, Any],
) -> None:
    if entry.id is None:
        await db.flush()  # assigns a new entry's id
    db.add(
        LibraryEvent(
            user_id=entry.user_id,
            user_book_id=entry.id,
            event_type=event_type,
            changed_fields=changes,
            request_id=current_request_id(),
        )
    )


async def record_created(db: AsyncSession, entry: UserBook) -> None:
    """A ``created`` event for the new entry ``entry``. Does not commit."""
    empty = dict.fromkeys(TRACKED_FIELDS)
    await _add(db, entry, EVENT_CREATED, changed_fields(empty, snapshot(entry)))


async def record_change(
    db: AsyncSession,
    entry: UserBook,
    before: Snapshot,
    event_type: str | None = None,
) -> bool:
    """One event for the change from ``before`` to ``entry`` as it is now;
    nothing when no tracked field changed. ``event_type`` defaults to
    ``status_changed`` when the status changed, else ``updated``. Returns
    whether an event was written. Does not commit."""
    changes = changed_fields(before, snapshot(entry))
    if not changes:
        return False
    if event_type is None:
        event_type = EVENT_STATUS_CHANGED if "status" in changes else EVENT_UPDATED
    await _add(db, entry, event_type, changes)
    return True


async def record_purged(
    db: AsyncSession, rows: list[tuple[uuid.UUID, uuid.UUID]]
) -> None:
    """A ``purged`` event for each hard-deleted library row, given as
    ``(user_book_id, user_id)`` pairs, in one statement. Does not commit."""
    if not rows:
        return
    request_id = current_request_id()
    await db.execute(
        insert(LibraryEvent),
        [
            {
                "user_id": user_id,
                "user_book_id": user_book_id,
                "event_type": EVENT_PURGED,
                "changed_fields": {},
                "request_id": request_id,
            }
            for user_book_id, user_id in rows
        ],
    )

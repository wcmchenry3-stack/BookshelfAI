import uuid

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models._mixins import CreatedAtMixin

EVENT_CREATED = "created"
EVENT_UPDATED = "updated"
EVENT_STATUS_CHANGED = "status_changed"
EVENT_DELETED = "deleted"
EVENT_RESTORED = "restored"
EVENT_PURGED = "purged"
# Reserved for book formats (#501, not built yet): allowed by the check
# constraint, never written today.
EVENT_FORMAT_ADDED = "format_added"
EVENT_FORMAT_REMOVED = "format_removed"

EVENT_TYPES = (
    EVENT_CREATED,
    EVENT_UPDATED,
    EVENT_STATUS_CHANGED,
    EVENT_DELETED,
    EVENT_RESTORED,
    EVENT_PURGED,
    EVENT_FORMAT_ADDED,
    EVENT_FORMAT_REMOVED,
)

_EVENT_TYPE_IN = "event_type IN ({})".format(", ".join(f"'{t}'" for t in EVENT_TYPES))


class LibraryEvent(CreatedAtMixin, Base):
    """One row per change to a user's library entry (#684): an append-only
    audit log, so support can say what happened to a book.

    Written by ``app.services.library_events`` in the same transaction as the
    change it records. The app only inserts: nothing updates a row, and only
    the retention job (``app.services.library_events_retention``) deletes,
    after ``settings.library_events_retention_days`` (180).
    ``tests/security/test_library_events_append_only.py`` enforces this, and the
    app role has no ``UPDATE`` on the table (migration 0030).

    ``changed_fields`` maps a field name to ``{"old": ..., "new": ...}`` for
    structured fields; free text (notes, title / author / cover overrides) is
    recorded only as ``"changed"``, never its value.

    ``user_id`` is ``ON DELETE SET NULL``: deleting an account anonymises its
    events rather than failing or deleting them. ``user_book_id`` has no
    foreign key, so the event outlives a purged library row.
    """

    __tablename__ = "library_events"
    __table_args__ = (
        CheckConstraint(_EVENT_TYPE_IN, name="ck_library_events_event_type"),
        # A user's history (support, export #694), newest first.
        Index("ix_library_events_user_created", "user_id", "created_at"),
        # The retention job deletes by age.
        Index("ix_library_events_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # The library row (``user_books.id``); deliberately no foreign key.
    user_book_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    changed_fields: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # The server-generated X-Request-ID (app.core.logging); NULL outside a
    # request (the purge job).
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

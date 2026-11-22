"""Library audit log: the library_events table (#684)

Expand only: a new table, its indexes, RLS and one privilege revoke. The
previous release never reads or writes it.

- ``library_events``: one append-only row per change to a library entry.

  Columns:
    id              BIGSERIAL PK
    user_id         UUID FK -> users.id ON DELETE SET NULL, nullable: deleting
                    an account anonymises its events instead of failing or
                    deleting them (#291 decides what else it does)
    user_book_id    UUID, nullable, no foreign key: the event outlives the
                    library row (the trash purge hard-deletes it)
    event_type      VARCHAR(32), CHECK ``ck_library_events_event_type``:
                    created, updated, status_changed, deleted, restored,
                    purged, format_added, format_removed (the last two are
                    reserved for formats, #501)
    changed_fields  JSONB NOT NULL DEFAULT '{}': {field: {old, new}}; free
                    text only as "changed"
    request_id      VARCHAR(64), nullable: the server's X-Request-ID
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()

  Indexes: ``ix_library_events_user_created`` (user_id, created_at) for a
  user's history and the SET NULL scan on account deletion;
  ``ix_library_events_created_at`` for the 180-day retention job.

- RLS enabled, no policies (#677), like every table.
- Grants: migration 0028's default privileges give ``bookshelf_app``
  SELECT, INSERT, UPDATE, DELETE on new tables. ``UPDATE`` is revoked here:
  the log is append-only. ``DELETE`` stays, for the retention job only
  (``app/services/library_events_retention.py``). The ``ON DELETE SET NULL``
  of ``user_id`` still works for the app role: Postgres runs referential
  actions as the table owner.

Downgrade drops the table; the audit history is lost.

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-22
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

_APP_ROLE = "bookshelf_app"

# Kept literal (not imported from app.models.library_event) so the migration
# stays what it was when written, whatever the model later says.
_EVENT_TYPES = (
    "created",
    "updated",
    "status_changed",
    "deleted",
    "restored",
    "purged",
    "format_added",
    "format_removed",
)


def upgrade() -> None:
    op.create_table(
        "library_events",
        sa.Column("id", sa.BigInteger, autoincrement=True, primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("user_book_id", UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column(
            "changed_fields",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN ({})".format(", ".join(f"'{t}'" for t in _EVENT_TYPES)),
            name="ck_library_events_event_type",
        ),
    )
    op.create_index(
        "ix_library_events_user_created", "library_events", ["user_id", "created_at"]
    )
    op.create_index("ix_library_events_created_at", "library_events", ["created_at"])
    op.execute("ALTER TABLE library_events ENABLE ROW LEVEL SECURITY")
    # Append-only for the app (#684). The role exists (0028 requires it);
    # guarded anyway so a database without it still migrates.
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_APP_ROLE}') THEN
                REVOKE UPDATE ON public.library_events FROM {_APP_ROLE};
            END IF;
        END
        $$
        """)


def downgrade() -> None:
    op.drop_index("ix_library_events_created_at", table_name="library_events")
    op.drop_index("ix_library_events_user_created", table_name="library_events")
    op.drop_table("library_events")

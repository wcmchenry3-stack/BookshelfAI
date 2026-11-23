"""Account data export: the account_exports table (#694)

Expand only: a new table, its indexes, RLS and explicit grants. The previous
release never reads or writes it.

- ``account_exports``: one row per request for a copy of the user's data,
  the export worker's queue and the record of where the file is and when
  its signed link expires.

  Columns:
    id            UUID PK
    user_id       UUID NOT NULL FK -> users.id ON DELETE CASCADE: the row
                  goes with the account (the stored object does not; account
                  deletion #291 deletes it, and the daily storage sweep
                  removes any object older than 8 days)
    status        VARCHAR(16) NOT NULL DEFAULT 'pending', CHECK
                  ``ck_account_exports_status``: pending, running, emailing
                  (link recorded, the email may have gone out), ready,
                  failed
    locale        VARCHAR(16) NOT NULL, the language of the email
    attempts      SMALLINT NOT NULL DEFAULT 0, CHECK
                  ``ck_account_exports_attempts`` (>= 0)
    claimed_at    TIMESTAMPTZ, the running attempt's lease start
    completed_at  TIMESTAMPTZ
    object_path   VARCHAR(255), the object inside the private bucket
    size_bytes    BIGINT, the JSON document's size
    delivery_url  TEXT, the signed link (a credential until expires_at),
                  kept so a retry re-sends the identical email
    expires_at    TIMESTAMPTZ, when the signed link stops working
    error_code    VARCHAR(64), a fixed code, never a message
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()

  Indexes: ``ix_account_exports_user_created`` (user_id, created_at) for the
  one-a-day cap and the cascade scan; ``ix_account_exports_open``
  (created_at) WHERE status IN ('pending', 'running', 'emailing') for the
  worker;
  ``ix_account_exports_expires_at`` for the 7-day purge.

- RLS enabled, no policies (#677), like every table.
- Grants: migration 0028's default privileges give ``bookshelf_app`` DML on
  new tables; granted explicitly here too, so the table works for the app
  whatever the migrating role's defaults: SELECT, INSERT, UPDATE (the worker
  moves a row through its states), DELETE (the purge). No sequence (UUID
  key). Nothing for ``anon`` / ``authenticated`` (#704).

Locking: a new table needs no lock on existing ones beyond a brief one on
``users`` for the foreign key; a 5 s ``lock_timeout`` makes it fail fast
instead of queueing behind a long transaction. Re-running after a failure
is safe: the table and each index are created only when missing.

Downgrade drops the table (guarded the same way): pending requests and the
record of the files are lost; the files themselves expire on their own and
the storage sweep of a later upgrade removes them.

Revision ID: 0034
Revises: 0033
Create Date: 2026-11-23
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

_TABLE = "account_exports"
_APP_ROLE = "bookshelf_app"
_LOCK_TIMEOUT = "5s"
# Kept literal (not imported from the model) so the migration stays what it
# was when written.
_STATUSES = ("pending", "running", "emailing", "ready", "failed")
_INDEXES = (
    ("ix_account_exports_user_created", ["user_id", "created_at"], None),
    (
        "ix_account_exports_open",
        ["created_at"],
        "status IN ('pending', 'running', 'emailing')",
    ),
    ("ix_account_exports_expires_at", ["expires_at"], None),
)


def _create_table() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("locale", sa.String(16), nullable=False),
        sa.Column("attempts", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("object_path", sa.String(255), nullable=True),
        sa.Column("size_bytes", sa.BigInteger, nullable=True),
        sa.Column("delivery_url", sa.Text, nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ({})".format(", ".join(f"'{s}'" for s in _STATUSES)),
            name="ck_account_exports_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_account_exports_attempts"),
    )


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    op.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
    if not inspector.has_table(_TABLE):
        _create_table()
        inspector = sa.inspect(op.get_bind())
    existing = {i["name"] for i in inspector.get_indexes(_TABLE)}
    for name, columns, where in _INDEXES:
        if name in existing:
            continue
        if where is None:
            op.create_index(name, _TABLE, columns)
        else:
            op.create_index(name, _TABLE, columns, postgresql_where=sa.text(where))
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    # The role exists (0028 requires it); guarded anyway so a database
    # without it still migrates.
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_APP_ROLE}') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON public.{_TABLE}
                    TO {_APP_ROLE};
            END IF;
        END
        $$
        """)


def downgrade() -> None:
    # Same guard and lock timeout as upgrade: a re-run after a partial
    # downgrade skips what is gone.
    inspector = sa.inspect(op.get_bind())
    op.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
    if inspector.has_table(_TABLE):
        op.drop_table(_TABLE)

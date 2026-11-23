"""Idempotency-Key store: the idempotency_keys table (#685)

Expand only: a new table, its indexes, RLS and explicit grants. The previous
release never reads or writes it.

- ``idempotency_keys``: one row per ``Idempotency-Key`` a user sent on a
  library or account write, with the response to replay on a retry.

  Columns:
    id                BIGSERIAL PK
    user_id           UUID NOT NULL FK -> users.id ON DELETE CASCADE: the
                      stored body is the user's data and goes with the account
    key               VARCHAR(255) NOT NULL, the client's key
    request_hash      VARCHAR(64) NOT NULL, SHA-256 hex of method, path,
                      If-Match and the canonical JSON body
    state             VARCHAR(16) NOT NULL, CHECK ``ck_idempotency_keys_state``:
                      pending, complete
    response_status   SMALLINT, NULL while pending
    response_body     TEXT, the JSON body; NULL while pending and for a 204.
                      CHECK ``ck_idempotency_keys_body_size``: at most
                      65,536 bytes
    response_headers  JSONB, ETag and Location only
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()

  Constraint ``uq_idempotency_keys_user_key`` (user_id, key): the claim's
  ``ON CONFLICT`` target, the lookup and the scan for the cascade.
  Index ``ix_idempotency_keys_created_at`` for the 24-hour purge.

- RLS enabled, no policies (#677), like every table.
- Grants: migration 0028's default privileges give ``bookshelf_app`` DML on
  new tables; granted explicitly here too, so the table works for the app
  whatever the migrating role's defaults: SELECT, INSERT, UPDATE (a pending
  row becomes complete), DELETE (release and purge) and the sequence's
  USAGE. Nothing for ``anon`` / ``authenticated`` (#704).

Locking: a new table needs no lock on existing ones beyond a brief one on
``users`` for the foreign key; a 5 s ``lock_timeout`` makes it fail fast
instead of queueing behind a long transaction. Re-running after a failure
is safe: the table and each index are created only when missing.

Downgrade drops the table (guarded the same way): only stored responses for
retries within the last day are lost, and the previous release never uses
them.

Revision ID: 0033
Revises: 0032
Create Date: 2026-11-23
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

_TABLE = "idempotency_keys"
_CREATED_INDEX = "ix_idempotency_keys_created_at"
_APP_ROLE = "bookshelf_app"
_LOCK_TIMEOUT = "5s"
# Kept literal (not imported from the model) so the migration stays what it
# was when written.
_MAX_BODY_BYTES = 65_536


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    op.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
    if not inspector.has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.BigInteger, autoincrement=True, primary_key=True),
            sa.Column(
                "user_id",
                UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("key", sa.String(255), nullable=False),
            sa.Column("request_hash", sa.String(64), nullable=False),
            sa.Column("state", sa.String(16), nullable=False),
            sa.Column("response_status", sa.SmallInteger, nullable=True),
            sa.Column("response_body", sa.Text, nullable=True),
            sa.Column("response_headers", JSONB, nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.UniqueConstraint("user_id", "key", name="uq_idempotency_keys_user_key"),
            sa.CheckConstraint(
                "state IN ('pending', 'complete')", name="ck_idempotency_keys_state"
            ),
            sa.CheckConstraint(
                "response_body IS NULL OR octet_length(response_body) <= "
                f"{_MAX_BODY_BYTES}",
                name="ck_idempotency_keys_body_size",
            ),
        )
        inspector = sa.inspect(op.get_bind())
    if _CREATED_INDEX not in {i["name"] for i in inspector.get_indexes(_TABLE)}:
        op.create_index(_CREATED_INDEX, _TABLE, ["created_at"])
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    # The role exists (0028 requires it); guarded anyway so a database
    # without it still migrates.
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_APP_ROLE}') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON public.{_TABLE}
                    TO {_APP_ROLE};
                GRANT USAGE ON SEQUENCE public.{_TABLE}_id_seq TO {_APP_ROLE};
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

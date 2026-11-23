"""Add user_books.version and user_custom_books.version (#685)

Optimistic concurrency for library entries: every update of a row bumps its
``version`` (SQLAlchemy ``version_id_col``, so the ORM writes
``UPDATE ... WHERE id = ? AND version = ?``). The API returns it in the body
and as the ``ETag`` of a single entry, and ``PATCH`` / ``DELETE
/user-books/{id}`` answer ``412`` when the client's ``If-Match`` names an
older version, so a stale device cannot overwrite a newer edit.

Expand only: ``INTEGER NOT NULL DEFAULT 1`` on both tables. With a constant
default, PostgreSQL 11+ stores the default in the catalog and does not
rewrite or scan the table (existing rows read as 1), so there is no backfill.
The previous release, still serving while this runs, never reads or writes
the column; its inserts get the default, and its updates leave it as it is
(the next update by the new release bumps it).

Locking: adding the column needs a brief ACCESS EXCLUSIVE lock on each table;
a 5 s ``lock_timeout`` makes it fail fast instead of queueing every query
behind a long transaction. Re-running after a failure is safe: each column is
added only when missing.

Downgrade drops both columns; nothing but the version counters is lost, and
the previous release does not use them.

Revision ID: 0032
Revises: 0031
Create Date: 2026-11-23
"""

import sqlalchemy as sa

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

_TABLES = ("user_books", "user_custom_books")
_COLUMN = "version"
_LOCK_TIMEOUT = "5s"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    missing = [
        table
        for table in _TABLES
        if _COLUMN not in {c["name"] for c in inspector.get_columns(table)}
    ]
    op.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
    for table in missing:
        op.add_column(
            table,
            sa.Column(
                _COLUMN, sa.Integer(), nullable=False, server_default=sa.text("1")
            ),
        )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_column(table, _COLUMN)

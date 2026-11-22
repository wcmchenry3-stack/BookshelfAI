"""Soft delete with a 30-day trash (#683)

Expand only:

- ``user_books.deleted_at`` and ``user_custom_books.deleted_at``: nullable
  ``timestamptz``. ``DELETE /user-books/{id}`` sets them (the library row
  and, for an unverified entry, its ``user_custom_books`` row) instead of
  deleting; ``POST /user-books/{id}/restore`` clears them; the daily cleanup
  hard-deletes rows trashed more than ``TRASH_RETENTION_DAYS`` (30) ago.
  The previous release, still serving while this runs, never reads or
  writes the column (it hard-deletes, which stays valid).
- The unique constraint ``uq_user_books_user_book (user_id, book_id)`` is
  replaced by the partial unique index ``uq_user_books_user_book_live`` on
  the same columns ``WHERE deleted_at IS NULL``, so a user can add a book
  again while an older entry of it is in the trash, and two live entries of
  one book are still refused. The index is built before the constraint is
  dropped. This is not destructive: no row or column goes, and while the
  previous release is live no row has ``deleted_at`` set, so the index
  enforces exactly what the constraint did. (The migration guard does not
  flag dropping a constraint, since no data is lost; noted here for the
  reviewer as the plan asked.)
- ``ix_user_books_deleted_at``, a partial index on ``user_books
  (deleted_at) WHERE deleted_at IS NOT NULL``, for the purge (rows trashed
  before the cutoff) and the trash list. It holds trashed rows only.
  ``user_custom_books`` is never purged on its own (its rows go with their
  library row), so it gets no index.
- ``uq_user_books_custom_book`` stays: an unverified entry belongs to exactly
  one library row and is trashed and restored with it.

Locks: the unique index is built without ``CONCURRENTLY`` (a SHARE lock
on ``user_books``: reads run, writes wait) and ``DROP CONSTRAINT`` takes an
ACCESS EXCLUSIVE lock; both are brief at the table's current size.

Deploy overlap: while the previous release is still serving, its instances
keep hard-deleting on ``DELETE`` and read trashed rows as live (they do not
know ``deleted_at``) until they stop. Entries trashed by the new release in
that window show up in the old instances' library reads; nothing is lost.

Downgrade loses the trash: trashed rows are hard-deleted first, because a
trashed entry and a live entry of the same book would block the full unique
constraint. Only ``user_custom_books`` rows referenced by a trashed
``user_books`` row (their cascade removes exactly that row), and trashed
custom books no ``user_books`` row references, are deleted, so the cascade
never reaches a live library row; then the remaining trashed ``user_books``.
Then the constraint comes back and the indexes and columns are dropped.

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-28
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

_user_books = sa.table(
    "user_books",
    sa.column("id", UUID(as_uuid=True)),
    sa.column("custom_book_id", UUID(as_uuid=True)),
    sa.column("deleted_at", sa.DateTime(timezone=True)),
)
_user_custom_books = sa.table(
    "user_custom_books",
    sa.column("id", UUID(as_uuid=True)),
    sa.column("deleted_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    op.add_column(
        "user_books",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "user_custom_books",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "uq_user_books_user_book_live",
        "user_books",
        ["user_id", "book_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_constraint("uq_user_books_user_book", "user_books", type_="unique")
    op.create_index(
        "ix_user_books_deleted_at",
        "user_books",
        ["deleted_at"],
        postgresql_where=sa.text("deleted_at IS NOT NULL"),
    )


def purge_trash(conn) -> None:
    """Hard-delete every trashed entry (downgrade only): the unverified
    entries of trashed rows (cascading to exactly those rows) and trashed
    ``user_custom_books`` rows that no library row references, then the
    remaining trashed ``user_books``. Never a custom book a live library row
    references, so the cascade never removes a live entry."""
    trashed_custom = sa.select(_user_books.c.custom_book_id).where(
        _user_books.c.deleted_at.isnot(None),
        _user_books.c.custom_book_id.isnot(None),
    )
    referenced = sa.select(_user_books.c.custom_book_id).where(
        _user_books.c.custom_book_id.isnot(None)
    )
    conn.execute(
        sa.delete(_user_custom_books).where(
            sa.or_(
                _user_custom_books.c.id.in_(trashed_custom),
                sa.and_(
                    _user_custom_books.c.deleted_at.isnot(None),
                    _user_custom_books.c.id.not_in(referenced),
                ),
            )
        )
    )
    conn.execute(sa.delete(_user_books).where(_user_books.c.deleted_at.isnot(None)))


def downgrade() -> None:
    purge_trash(op.get_bind())
    op.drop_index("ix_user_books_deleted_at", table_name="user_books")
    op.create_unique_constraint(
        "uq_user_books_user_book", "user_books", ["user_id", "book_id"]
    )
    op.drop_index("uq_user_books_user_book_live", table_name="user_books")
    op.drop_column("user_custom_books", "deleted_at")
    op.drop_column("user_books", "deleted_at")

"""user_custom_books: private unverified entries (#616)

Expand only. A book neither Open Library nor Google Books finds can be added
by the user as an **unverified** entry. It is stored in a new per-user table,
``user_custom_books`` (title, author, an optional ISBN-13 the user typed; no
provider IDs, no cover), outside the shared catalog: ``books`` and
``editions`` are unchanged, so no catalog lookup or library check can match
another user's entry, and the user's ISBN never claims the globally unique
``editions.isbn_13``.

``user_books`` gains ``custom_book_id`` (FK ``user_custom_books``, ON DELETE
CASCADE: both rows are the same user's data; unique, one library row per
entry), ``book_id`` becomes nullable, and ``ck_user_books_one_book`` requires
exactly one of the two. Existing rows all have a ``book_id`` and no
``custom_book_id``, so they satisfy the check and nothing is rewritten.
``user_custom_books.user_id`` cascades with the account like ``user_books``.

Downgrade deletes every unverified entry (the ``user_books`` rows that point
at one, then the table) before ``book_id`` can be NOT NULL again: those
entries have no catalog book to fall back to.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-23
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

# Frozen copies of the model limits at this revision.
_TITLE_MAX = 500
_AUTHOR_MAX = 500


def upgrade() -> None:
    op.create_table(
        "user_custom_books",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("author", sa.String(), nullable=False),
        sa.Column("isbn_13", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            f"char_length(title) BETWEEN 1 AND {_TITLE_MAX}",
            name="ck_user_custom_books_title",
        ),
        sa.CheckConstraint(
            f"char_length(author) BETWEEN 1 AND {_AUTHOR_MAX}",
            name="ck_user_custom_books_author",
        ),
        sa.CheckConstraint(
            "isbn_13 IS NULL OR char_length(isbn_13) = 13",
            name="ck_user_custom_books_isbn_13",
        ),
    )
    op.create_index(
        op.f("ix_user_custom_books_user_id"), "user_custom_books", ["user_id"]
    )

    op.add_column(
        "user_books",
        sa.Column("custom_book_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "user_books_custom_book_id_fkey",
        "user_books",
        "user_custom_books",
        ["custom_book_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_user_books_custom_book", "user_books", ["custom_book_id"]
    )
    op.alter_column("user_books", "book_id", nullable=True)
    op.create_check_constraint(
        "ck_user_books_one_book",
        "user_books",
        "(book_id IS NULL) <> (custom_book_id IS NULL)",
    )


def downgrade() -> None:
    # Unverified entries have no catalog book, so they cannot survive
    # book_id becoming NOT NULL again (see the docstring).
    op.execute("DELETE FROM user_books WHERE custom_book_id IS NOT NULL")
    op.drop_constraint("ck_user_books_one_book", "user_books", type_="check")
    op.alter_column("user_books", "book_id", nullable=False)
    op.drop_constraint("uq_user_books_custom_book", "user_books", type_="unique")
    op.drop_constraint(
        "user_books_custom_book_id_fkey", "user_books", type_="foreignkey"
    )
    op.drop_column("user_books", "custom_book_id")
    op.drop_index(op.f("ix_user_custom_books_user_id"), table_name="user_custom_books")
    op.drop_table("user_custom_books")

"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-03-21
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- users ---
    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("google_id", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("avatar_url", sa.String(), nullable=True),
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
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.UniqueConstraint("google_id", name="uq_users_google_id"),
    )

    # --- books ---
    op.create_table(
        "books",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("open_library_work_id", sa.String(), nullable=True),
        sa.Column("google_books_id", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("author", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("cover_url", sa.String(), nullable=True),
        sa.Column("subjects", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("language", sa.String(), nullable=False, server_default="en"),
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
        sa.UniqueConstraint(
            "open_library_work_id", name="uq_books_open_library_work_id"
        ),
    )
    op.create_index("idx_books_open_library_work_id", "books", ["open_library_work_id"])

    # --- editions ---
    op.create_table(
        "editions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "book_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("books.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("isbn_13", sa.String(), nullable=True),
        sa.Column("isbn_10", sa.String(), nullable=True),
        sa.Column("publisher", sa.String(), nullable=True),
        sa.Column("publish_year", sa.Integer(), nullable=True),
        sa.Column("format", sa.String(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("open_library_edition_id", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("isbn_13", name="uq_editions_isbn_13"),
        sa.CheckConstraint(
            "format IN ('hardcover', 'paperback', 'ebook', 'audiobook', 'unknown')",
            name="ck_editions_format",
        ),
    )
    op.create_index("idx_editions_book_id", "editions", ["book_id"])

    # --- user_books ---
    op.create_table(
        "user_books",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "book_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("books.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "edition_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("editions.id"),
            nullable=True,
        ),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("wishlisted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purchased_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("rating", sa.Integer(), nullable=True),
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
        sa.UniqueConstraint("user_id", "book_id", name="uq_user_books_user_book"),
        sa.CheckConstraint(
            "status IN ('wishlisted', 'purchased', 'reading', 'read')",
            name="ck_user_books_status",
        ),
        sa.CheckConstraint(
            "rating BETWEEN 1 AND 5",
            name="ck_user_books_rating",
        ),
    )
    op.create_index("idx_user_books_user_id", "user_books", ["user_id"])
    op.create_index("idx_user_books_status", "user_books", ["status"])


def downgrade() -> None:
    op.drop_table("user_books")
    op.drop_table("editions")
    op.drop_table("books")
    op.drop_table("users")

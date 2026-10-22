"""restrict catalog deletes: books -> user_books / editions

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-21

Deleting a shared catalog row (books) must never remove user data. The FKs
from user_books.book_id and editions.book_id change from ON DELETE CASCADE to
ON DELETE RESTRICT. user_books.user_id stays CASCADE (account deletion).
user_books.edition_id is unchanged.
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_FKS = [
    ("user_books_book_id_fkey", "user_books"),
    ("editions_book_id_fkey", "editions"),
]


def _recreate(ondelete: str) -> None:
    for name, table in _FKS:
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(
            name, table, "books", ["book_id"], ["id"], ondelete=ondelete
        )


def upgrade() -> None:
    _recreate("RESTRICT")


def downgrade() -> None:
    _recreate("CASCADE")

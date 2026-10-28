"""Catalog/user split: user overrides, book provenance, merged_into_id (#682)

Additive (expand only):

- ``user_books.title_override``, ``author_override``, ``cover_url_override``:
  nullable, the user's own values for their library entry, never written to
  ``books``. Check constraints bound their lengths (500 / 500 / 2048, the
  catalog's limits) and keep them off unverified entries (#616).
- ``books.source`` (nullable, check: ``google_books``, ``open_library``,
  ``scan``) and ``books.fetched_at`` (nullable): provenance of the row's
  data. Nullable in this release so the previous release, still serving
  while this runs, can insert books (its rows have NULL; a later contract
  migration backfills them and sets NOT NULL).
- ``books.merged_into_id``: self-FK ``ON DELETE RESTRICT``, indexed
  (``ix_books_merged_into_id``, for "rows merged into X"), with a check that
  a row is never merged into itself. No row is merged yet: the only merge
  (0015) deleted its duplicates.

Backfill of existing books, in one ``UPDATE`` (alembic runs the whole
migration in one transaction, so batches would not shorten any lock):
``source`` is ``google_books`` when ``google_books_id`` is set, else
``open_library`` when ``open_library_work_id`` is set, else ``scan`` (no
provider matched: a row from before #602); ``fetched_at`` is ``created_at``. The rule is
``app.services.catalog.source_of``, frozen here.

Downgrade drops the new columns, constraints and index. The override values
users entered and the provenance are lost (``books`` itself keeps every
catalog value: nothing here ever changed one).

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-27
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

# Frozen copy of app.models.book.BOOK_SOURCES at this revision.
_SOURCES = ("google_books", "open_library", "scan")
_OVERRIDES = (
    ("title_override", 500),
    ("author_override", 500),
    ("cover_url_override", 2048),
)

_books = sa.table(
    "books",
    sa.column("id", UUID(as_uuid=True)),
    sa.column("google_books_id", sa.String),
    sa.column("open_library_work_id", sa.String),
    sa.column("source", sa.String),
    sa.column("fetched_at", sa.DateTime(timezone=True)),
    sa.column("created_at", sa.DateTime(timezone=True)),
)


def backfill_provenance(conn) -> int:
    """Set ``source`` and ``fetched_at`` on every book with no ``source``,
    in one statement. Returns the number of rows set."""
    source = sa.case(
        (_books.c.google_books_id.isnot(None), sa.literal("google_books")),
        (_books.c.open_library_work_id.isnot(None), sa.literal("open_library")),
        else_=sa.literal("scan"),
    )
    return conn.execute(
        sa.update(_books)
        .where(_books.c.source.is_(None))
        .values(
            source=source,
            fetched_at=sa.func.coalesce(_books.c.fetched_at, _books.c.created_at),
        )
    ).rowcount


def upgrade() -> None:
    for name, _limit in _OVERRIDES:
        op.add_column("user_books", sa.Column(name, sa.String(), nullable=True))
    for name, limit in _OVERRIDES:
        op.create_check_constraint(
            f"ck_user_books_{name}",
            "user_books",
            f"{name} IS NULL OR char_length({name}) BETWEEN 1 AND {limit}",
        )
    op.create_check_constraint(
        "ck_user_books_overrides_catalog_only",
        "user_books",
        "custom_book_id IS NULL OR (title_override IS NULL "
        "AND author_override IS NULL AND cover_url_override IS NULL)",
    )

    op.add_column("books", sa.Column("source", sa.String(), nullable=True))
    op.add_column(
        "books", sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "books", sa.Column("merged_into_id", UUID(as_uuid=True), nullable=True)
    )
    op.create_foreign_key(
        "books_merged_into_id_fkey",
        "books",
        "books",
        ["merged_into_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        op.f("ix_books_merged_into_id"), "books", ["merged_into_id"], unique=False
    )
    op.create_check_constraint(
        "ck_books_source", "books", f"source IS NULL OR source IN {_SOURCES}"
    )
    op.create_check_constraint(
        "ck_books_merged_into_other",
        "books",
        "merged_into_id IS NULL OR merged_into_id <> id",
    )
    backfill_provenance(op.get_bind())


def downgrade() -> None:
    op.drop_constraint("ck_books_merged_into_other", "books", type_="check")
    op.drop_constraint("ck_books_source", "books", type_="check")
    op.drop_index(op.f("ix_books_merged_into_id"), table_name="books")
    op.drop_constraint("books_merged_into_id_fkey", "books", type_="foreignkey")
    op.drop_column("books", "merged_into_id")
    op.drop_column("books", "fetched_at")
    op.drop_column("books", "source")

    op.drop_constraint(
        "ck_user_books_overrides_catalog_only", "user_books", type_="check"
    )
    for name, _limit in reversed(_OVERRIDES):
        op.drop_constraint(f"ck_user_books_{name}", "user_books", type_="check")
        op.drop_column("user_books", name)

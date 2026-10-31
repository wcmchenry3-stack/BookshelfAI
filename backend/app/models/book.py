import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models._mixins import IdMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.edition import Edition
    from app.models.user_book import UserBook

# Where a catalog row's data came from (#682), ``books.source``:
# - ``google_books``: a Google Books volume matched (``google_books_id`` set).
#   The description, cover and edition details come from it.
# - ``open_library``: only an Open Library work matched.
# - ``scan``: no provider matched; the title and author are a scan's (or a
#   search query's) reading. Only rows written before #602 can be this: since
#   then a book with no provider ID is never created.
# NULL only for a row written by an older release (the column is nullable
# until a contract migration, see migration 0025).
BOOK_SOURCES = ("google_books", "open_library", "scan")


class Book(IdMixin, TimestampMixin, Base):
    __tablename__ = "books"
    __table_args__ = (
        # One row per Google Books volume (#613, migration 0015, which merged
        # the duplicates that existed before). Partial: most books have none.
        Index(
            "uq_books_google_books_id",
            "google_books_id",
            unique=True,
            postgresql_where=text("google_books_id IS NOT NULL"),
        ),
        CheckConstraint(
            f"source IS NULL OR source IN {BOOK_SOURCES}",
            name="ck_books_source",
        ),
        CheckConstraint(
            "merged_into_id IS NULL OR merged_into_id <> id",
            name="ck_books_merged_into_other",
        ),
    )

    open_library_work_id: Mapped[str | None] = mapped_column(
        String, unique=True, nullable=True, index=True
    )
    google_books_id: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    author: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    cover_url: Mapped[str | None] = mapped_column(String, nullable=True)
    subjects: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    language: Mapped[str] = mapped_column(String, default="en", nullable=False)
    # Provenance (#682): which provider supplied the row (BOOK_SOURCES) and
    # when the server fetched that data. Set by every catalog write
    # (``source_of`` in app/services/catalog.py); backfilled by migration 0025.
    source: Mapped[str | None] = mapped_column(String, nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Set when this row was merged into another (#682): a duplicate of the
    # work. Reads follow it to the surviving row and no new library entry
    # ever targets a merged row (app/services/catalog.py). Always points at a
    # row that is not itself merged (a merge re-points earlier merges).
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("books.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    # Deleting a Book never deletes or orphans children: passive_deletes="all"
    # stops the ORM from nulling out loaded children's book_id, so the
    # ON DELETE RESTRICT FKs reject the delete.
    editions: Mapped[list["Edition"]] = relationship(
        "Edition", back_populates="book", passive_deletes="all"
    )
    user_books: Mapped[list["UserBook"]] = relationship(
        "UserBook", back_populates="book", passive_deletes="all"
    )
    # The surviving row (#682). Never lazy-loaded: callers that may meet a
    # merged row load it explicitly (``selectinload``) or resolve it with
    # app/services/catalog.py.
    merged_into: Mapped["Book | None"] = relationship(
        "Book", remote_side="Book.id", foreign_keys=[merged_into_id], lazy="raise"
    )

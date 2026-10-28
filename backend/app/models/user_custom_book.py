import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# Field limits, shared with the request schema (app/schemas/user_book.py) and
# enforced again by the table's check constraints. The title and author limits
# are the catalog's (BookBase / WishlistRequest).
TITLE_MAX = 500
AUTHOR_MAX = 500


class UserCustomBook(Base):
    """A book the user added themselves because neither Open Library nor
    Google Books found it: an **unverified** entry (#616).

    Private to its creator by construction. It lives outside the shared
    catalog (``books`` / ``editions``), so no catalog lookup, library check
    (``held_entries``), scan, search or wishlist add can see it: those only
    query the catalog tables. Only the creator's own ``user_books`` row
    references it (``user_books.custom_book_id``), and the app reads it only
    through that row, scoped to the session's user.

    It holds no provider IDs, no cover and no shared ISBN: ``isbn_13`` is the
    user's own note of the ISBN, not unique and never matched against anyone
    else's data. At most ``settings.unverified_books_per_user_max`` rows per
    user. Rows go with the account (ON DELETE CASCADE) and with the library
    entry: ``DELETE /user-books/{id}`` moves both to the trash
    (``deleted_at``, #683), restore brings both back, and the daily purge
    deletes both once the trash window has passed (or the per-user trash
    cap evicts them). Trashed rows do not count
    towards the cap or the duplicate check. Account deletion and export (not
    built yet) must include this table, trashed rows included.
    """

    __tablename__ = "user_custom_books"
    __table_args__ = (
        CheckConstraint(
            f"char_length(title) BETWEEN 1 AND {TITLE_MAX}",
            name="ck_user_custom_books_title",
        ),
        CheckConstraint(
            f"char_length(author) BETWEEN 1 AND {AUTHOR_MAX}",
            name="ck_user_custom_books_author",
        ),
        CheckConstraint(
            "isbn_13 IS NULL OR char_length(isbn_13) = 13",
            name="ck_user_custom_books_isbn_13",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    author: Mapped[str] = mapped_column(String, nullable=False)
    isbn_13: Mapped[str | None] = mapped_column(String, nullable=True)
    # Soft delete (#683): always set and cleared together with the library
    # row that holds this entry (user_books.deleted_at).
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    @classmethod
    def live(cls):
        """The filter for entries not in the trash (#683)."""
        return cls.deleted_at.is_(None)

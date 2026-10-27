import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

VALID_STATUSES = ("wishlisted", "purchased", "reading", "read")

# The user's own corrections of a catalog book (#682), per library entry.
# Limits match the catalog's title / author (BookBase, WishlistRequest) and
# cover_url fields; the request schema (UserBookUpdate) enforces them too.
TITLE_OVERRIDE_MAX = 500
AUTHOR_OVERRIDE_MAX = 500
COVER_URL_OVERRIDE_MAX = 2048
# BookRead field -> override column.
OVERRIDE_COLUMNS = {
    "title": "title_override",
    "author": "author_override",
    "cover_url": "cover_url_override",
}


class UserBook(Base):
    __tablename__ = "user_books"
    __table_args__ = (
        UniqueConstraint("user_id", "book_id", name="uq_user_books_user_book"),
        # One unverified entry per library row (#616).
        UniqueConstraint("custom_book_id", name="uq_user_books_custom_book"),
        # Exactly one of a catalog book or the user's own unverified entry.
        CheckConstraint(
            "(book_id IS NULL) <> (custom_book_id IS NULL)",
            name="ck_user_books_one_book",
        ),
        CheckConstraint(
            f"status IN {VALID_STATUSES}",
            name="ck_user_books_status",
        ),
        CheckConstraint(
            "rating BETWEEN 1 AND 5",
            name="ck_user_books_rating",
        ),
        CheckConstraint(
            "title_override IS NULL OR char_length(title_override) "
            f"BETWEEN 1 AND {TITLE_OVERRIDE_MAX}",
            name="ck_user_books_title_override",
        ),
        CheckConstraint(
            "author_override IS NULL OR char_length(author_override) "
            f"BETWEEN 1 AND {AUTHOR_OVERRIDE_MAX}",
            name="ck_user_books_author_override",
        ),
        CheckConstraint(
            "cover_url_override IS NULL OR char_length(cover_url_override) "
            f"BETWEEN 1 AND {COVER_URL_OVERRIDE_MAX}",
            name="ck_user_books_cover_url_override",
        ),
        # Overrides correct a catalog book; an unverified entry (#616) is
        # already the user's own data and never carries a cover.
        CheckConstraint(
            "custom_book_id IS NULL OR (title_override IS NULL "
            "AND author_override IS NULL AND cover_url_override IS NULL)",
            name="ck_user_books_overrides_catalog_only",
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
    # The shared catalog book; NULL only for an unverified entry (#616),
    # which has ``custom_book_id`` instead (ck_user_books_one_book).
    book_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("books.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # The user's own unverified entry (#616, ``user_custom_books``), private
    # to this user. Both rows are user data, so the cascade is safe.
    custom_book_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_custom_books.id", ondelete="CASCADE"),
        nullable=True,
    )
    edition_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("editions.id"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String, nullable=False, index=True)
    wishlisted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    purchased_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The user's own title / author / cover for this copy (#682). Private to
    # this entry: never written to ``books``. Library reads return them in
    # place of the catalog values (UserBookRead); NULL = the catalog value.
    title_override: Mapped[str | None] = mapped_column(String, nullable=True)
    author_override: Mapped[str | None] = mapped_column(String, nullable=True)
    cover_url_override: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    book: Mapped["Book | None"] = relationship(  # noqa: F821
        "Book", back_populates="user_books"
    )
    custom_book: Mapped["UserCustomBook | None"] = relationship(  # noqa: F821
        "UserCustomBook"
    )

    @property
    def unverified(self) -> bool:
        """Whether this is the user's own unverified entry (#616)."""
        return self.custom_book_id is not None

    edition: Mapped["Edition | None"] = relationship("Edition")  # noqa: F821

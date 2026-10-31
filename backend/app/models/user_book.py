import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    and_,
    or_,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models._mixins import IdMixin, TimestampMixin, UserOwnedMixin

VALID_STATUSES = ("wishlisted", "purchased", "reading", "read")
# The status a new entry starts with (POST /wishlist, unverified adds).
DEFAULT_STATUS = "wishlisted"

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


class UserBook(IdMixin, UserOwnedMixin, TimestampMixin, Base):
    __tablename__ = "user_books"
    user_id_indexed = True
    __table_args__ = (
        # One live entry per user and book (#683): an entry in the trash
        # (``deleted_at`` set) does not block adding the book again.
        Index(
            "uq_user_books_user_book_live",
            "user_id",
            "book_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # The trash list and the purge read trashed rows only (#683).
        Index(
            "ix_user_books_deleted_at",
            "deleted_at",
            postgresql_where=text("deleted_at IS NOT NULL"),
        ),
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
    # Soft delete (#683): set by DELETE /user-books/{id}, cleared by
    # POST /user-books/{id}/restore; the daily cleanup hard-deletes rows
    # trashed more than settings.trash_retention_days ago. Every library
    # query except the trash, restore and purge must filter on
    # ``UserBook.live()``.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    book: Mapped["Book | None"] = relationship(  # noqa: F821
        "Book", back_populates="user_books"
    )
    custom_book: Mapped["UserCustomBook | None"] = relationship(  # noqa: F821
        "UserCustomBook"
    )

    @classmethod
    def live(cls):
        """The filter for entries not in the trash (#683)."""
        return cls.deleted_at.is_(None)

    @classmethod
    def has_no_user_data(cls):
        """The filter for entries that hold nothing the user set (#683), so
        same-book eviction may replace them (``trash.evict_same_book``):
        status still :data:`DEFAULT_STATUS`; no status dates
        (``purchased_at``, ``started_at``, ``finished_at``, set by moving
        the entry past wishlisted, kept if it moves back); notes NULL or
        only whitespace; no rating, edition or overrides. ``wishlisted_at``
        is set by the server on every add, so it is not user data. An
        unverified entry's title, author and ISBN (``user_custom_books``)
        are the book itself, not user data. Add a column here when you add
        a user-set column to this table."""
        return and_(
            cls.status == DEFAULT_STATUS,
            cls.purchased_at.is_(None),
            cls.started_at.is_(None),
            cls.finished_at.is_(None),
            or_(cls.notes.is_(None), cls.notes.regexp_match(r"^\s*$")),
            cls.rating.is_(None),
            cls.edition_id.is_(None),
            cls.title_override.is_(None),
            cls.author_override.is_(None),
            cls.cover_url_override.is_(None),
        )

    @property
    def unverified(self) -> bool:
        """Whether this is the user's own unverified entry (#616)."""
        return self.custom_book_id is not None

    edition: Mapped["Edition | None"] = relationship("Edition")  # noqa: F821

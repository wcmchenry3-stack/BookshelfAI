import uuid
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models._mixins import CreatedAtMixin, IdMixin

if TYPE_CHECKING:
    from app.models.book import Book

VALID_FORMATS = ("hardcover", "paperback", "ebook", "audiobook", "unknown")


class Edition(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "editions"
    __table_args__ = (
        CheckConstraint(
            f"format IN {VALID_FORMATS}",
            name="ck_editions_format",
        ),
    )

    book_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("books.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    isbn_13: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    isbn_10: Mapped[str | None] = mapped_column(String, nullable=True)
    publisher: Mapped[str | None] = mapped_column(String, nullable=True)
    publish_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    format: Mapped[str | None] = mapped_column(String, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    open_library_edition_id: Mapped[str | None] = mapped_column(String, nullable=True)

    book: Mapped["Book"] = relationship("Book", back_populates="editions")

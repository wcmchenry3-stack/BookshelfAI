import uuid
from datetime import datetime

from sqlalchemy import ARRAY, DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Book(Base):
    __tablename__ = "books"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Deleting a Book never deletes children: the ON DELETE RESTRICT FKs decide.
    editions: Mapped[list["Edition"]] = relationship(  # noqa: F821
        "Edition", back_populates="book", passive_deletes=True
    )
    user_books: Mapped[list["UserBook"]] = relationship(  # noqa: F821
        "UserBook", back_populates="book", passive_deletes=True
    )

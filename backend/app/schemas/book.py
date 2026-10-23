from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, computed_field


class EditionBase(BaseModel):
    isbn_13: str | None = Field(None, max_length=13)
    isbn_10: str | None = Field(None, max_length=10)
    publisher: str | None = Field(None, max_length=500)
    publish_year: int | None = None
    format: (
        Literal["hardcover", "paperback", "ebook", "audiobook", "unknown"] | None
    ) = None
    page_count: int | None = None
    open_library_edition_id: str | None = Field(None, max_length=50)


class EditionRead(EditionBase):
    """Edition stored in DB — has id and book_id."""

    id: uuid.UUID
    book_id: uuid.UUID
    created_at: datetime

    model_config = {"from_attributes": True}


class EditionPreview(EditionBase):
    """Edition from scan results — not yet persisted, no id/book_id."""


class BookBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    author: str = Field(..., min_length=1, max_length=500)
    description: str | None = Field(None, max_length=5000)
    cover_url: str | None = Field(None, max_length=2048)
    subjects: list[str] | None = None
    language: str = Field("en", max_length=10)


class BookRead(BookBase):
    id: uuid.UUID
    open_library_work_id: str | None = None
    google_books_id: str | None = None
    created_at: datetime
    updated_at: datetime
    editions: list[EditionRead] = []

    model_config = {"from_attributes": True}


class EnrichedBook(BaseModel):
    """Returned by POST /scan and GET /books/search."""

    book_id: uuid.UUID | None = None  # null if not yet in DB
    open_library_work_id: str | None = Field(None, max_length=50)
    google_books_id: str | None = Field(None, max_length=50)
    title: str = Field(..., max_length=500)
    author: str = Field(..., max_length=500)
    description: str | None = Field(None, max_length=5000)
    cover_url: str | None = Field(None, max_length=2048)
    subjects: list[str] = []
    confidence: float  # 0–1, from image recognition
    already_in_library: bool = False
    editions: list[EditionPreview] = []

    @computed_field  # type: ignore[prop-decorator]
    @property
    def in_library(self) -> bool:
        """Whether the user already has this book (#585): the same work-level
        match as ``already_in_library`` (Open Library work ID or Google Books
        ID), checked against the library on every scan, repeats included.
        Read-only; edition-level matching is #587."""
        return self.already_in_library


class ScanResponse(BaseModel):
    """Returned by POST /scan — one entry per distinct book found in the photo."""

    books: list[EnrichedBook] = []
    enhanced: bool = False  # True when the stronger model was used
    # Enhanced scans the user can still make today after this scan: the
    # shared daily allowance left, since an enhanced scan uses the shared
    # daily unit (#582; there is no enhanced monthly allowance). None for
    # users who are not Premium (enhanced scans are Premium-only, #533).
    enhanced_scans_remaining: int | None = None
    # True when the answer came from the user's saved scan record for this
    # exact photo (#585): no AI call and nothing charged.
    repeat: bool = False
    # A standard scan where the model answered but found no book (#577):
    # "free" = it did not use a scan (moved to the no-match allowance);
    # "counted" = it used a scan because today's no-match allowance is used
    # up. None otherwise (books found, enhanced, a repeat, or not known).
    no_match: Literal["free", "counted"] | None = None

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


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


class ScanResponse(BaseModel):
    """Returned by POST /scan — one entry per distinct book found in the photo."""

    books: list[EnrichedBook] = []
    enhanced: bool = False  # True when the stronger model was used
    enhanced_scan_credits: int  # credits remaining after this scan

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.core.url_validator import validate_safe_url
from app.schemas.book import BookRead, EditionPreview, EditionRead

BookStatus = Literal["wishlisted", "purchased", "reading", "read"]


class WishlistRequest(BaseModel):
    """Body for POST /wishlist — full book data from scan (book may not exist in DB yet)."""

    open_library_work_id: str | None = Field(None, max_length=50)
    google_books_id: str | None = Field(None, max_length=50)
    title: str = Field(..., min_length=1, max_length=500)
    author: str = Field(..., min_length=1, max_length=500)
    description: str | None = Field(None, max_length=5000)
    cover_url: str | None = Field(None, max_length=2048)
    subjects: list[str] = []
    editions: list[EditionPreview] = []

    @field_validator("cover_url", mode="before")
    @classmethod
    def cover_url_must_be_safe(cls, v: str | None) -> str | None:
        return validate_safe_url(v)


class UserBookCreate(BaseModel):
    """Body for POST /wishlist — book already exists in DB."""

    book_id: uuid.UUID
    edition_id: uuid.UUID | None = None


class PurchasedCreate(BaseModel):
    """Body for POST /purchased — promotes wishlist item or adds new purchased book."""

    book_id: uuid.UUID
    isbn_13: str | None = None
    edition_id: uuid.UUID | None = None


class UserBookUpdate(BaseModel):
    """Body for PATCH /user-books/{id}."""

    status: BookStatus | None = None
    notes: str | None = Field(None, max_length=10000)
    rating: int | None = None

    @field_validator("rating")
    @classmethod
    def rating_in_range(cls, v: int | None) -> int | None:
        if v is not None and not (1 <= v <= 5):
            raise ValueError("rating must be between 1 and 5")
        return v


class UserBookRead(BaseModel):
    """Returned by GET /user-books."""

    id: uuid.UUID
    book: BookRead
    edition: EditionRead | None = None
    status: BookStatus
    wishlisted_at: datetime | None = None
    purchased_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    notes: str | None = None
    rating: int | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

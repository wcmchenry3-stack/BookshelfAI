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
    # None for a user's own unverified entry (#616): no catalog language.
    language: str | None = Field("en", max_length=10)
    id: uuid.UUID
    open_library_work_id: str | None = None
    google_books_id: str | None = None
    created_at: datetime
    updated_at: datetime
    editions: list[EditionRead] = []

    model_config = {"from_attributes": True}


# Whether the requesting user already has a book, and which edition (#587):
# the result's edition (``editions[0]``, the one POST /wishlist would add)
# has the same ISBN-13 (ISBN-10 normalised) as an edition in their library;
# the work is in their library in a different edition; the work is there but
# one side has no ISBN to compare; or it is not in their library.
Ownership = Literal[
    "owned_same_edition",
    "owned_other_edition",
    "owned_edition_unknown",
    "not_owned",
]
NOT_OWNED: Ownership = "not_owned"


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
    # Edition-level library match for this user (#587). Per user and live:
    # never stored in a scan record. ``already_in_library`` / ``in_library``
    # stay true whenever this is not ``not_owned``, for older clients.
    ownership: Ownership = NOT_OWNED
    editions: list[EditionPreview] = []
    # Server signature over the catalog fields and the requesting user
    # (#602, app/core/catalog_token.py). Opaque to the app, which posts it
    # back unchanged with the book to POST /wishlist. Never stored in a
    # scan record: every response signs afresh.
    catalog_token: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def in_library(self) -> bool:
        """Whether the user already has this book (#585): the same value as
        ``already_in_library``, true whenever ``ownership`` is not
        ``not_owned`` (#587), checked against the library on every scan,
        repeats included. Read-only."""
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

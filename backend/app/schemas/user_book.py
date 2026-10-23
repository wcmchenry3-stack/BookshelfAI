from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.catalog_token import CATALOG_TOKEN_MAX_LENGTH
from app.core.url_validator import validate_safe_url
from app.models.user_custom_book import AUTHOR_MAX, TITLE_MAX
from app.schemas.book import BookRead, EditionPreview, EditionRead
from app.services.enrichment import isbn_10_to_13

BookStatus = Literal["wishlisted", "purchased", "reading", "read"]


class WishlistRequest(BaseModel):
    """Body for POST /wishlist — an ``EnrichedBook`` from /scan or
    /books/search, posted back with its ``catalog_token`` (#602). Only a
    valid token lets the server create ``books`` / ``editions`` rows from
    these fields; without one the request may only link an existing book."""

    open_library_work_id: str | None = Field(None, max_length=50)
    google_books_id: str | None = Field(None, max_length=50)
    title: str = Field(..., min_length=1, max_length=500)
    author: str = Field(..., min_length=1, max_length=500)
    description: str | None = Field(None, max_length=5000)
    cover_url: str | None = Field(None, max_length=2048)
    subjects: list[str] = []
    editions: list[EditionPreview] = []
    catalog_token: str | None = Field(None, max_length=CATALOG_TOKEN_MAX_LENGTH)

    @field_validator("cover_url", mode="before")
    @classmethod
    def cover_url_must_be_safe(cls, v: str | None) -> str | None:
        return validate_safe_url(v)


# Control characters (C0, DEL, C1): never part of a title, author or ISBN;
# NUL would also be refused by Postgres.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _isbn_13_valid(value: str) -> bool:
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(value[:12]))
    return (10 - total % 10) % 10 == int(value[12])


def _isbn_10_valid(value: str) -> bool:
    digits = [10 if c == "X" else int(c) for c in value]
    return sum((10 - i) * d for i, d in enumerate(digits)) % 11 == 0


def normalise_user_isbn(value: str) -> str:
    """A user-typed ISBN as an ISBN-13 (#616): hyphens and spaces dropped,
    an ISBN-10 converted (978 prefix). Raises ValueError unless it is a
    well-formed ISBN-13 or ISBN-10 with a correct check digit. Digits written
    in another script are read as the ASCII digits they stand for."""
    compact = "".join(
        # Decimal digits of any script (Arabic-Indic, fullwidth, ...) become
        # their ASCII digit, so the stored and compared ISBN is always ASCII.
        str(d) if (d := unicodedata.decimal(c, None)) is not None else c
        for c in value.replace("-", "").replace(" ", "").upper()
    )
    if len(compact) == 13 and compact.isdigit() and _isbn_13_valid(compact):
        return compact
    if (
        len(compact) == 10
        and compact[:9].isdigit()
        and (compact[9].isdigit() or compact[9] == "X")
        and _isbn_10_valid(compact)
    ):
        return isbn_10_to_13(compact)  # type: ignore[return-value]
    raise ValueError("isbn must be a valid ISBN-13 or ISBN-10")


class UnverifiedBookCreate(BaseModel):
    """Body for POST /wishlist/unverified (#616): a book the catalog could
    not find, added by the user as their own private, unverified entry.

    Only these fields exist. Anything else (provider IDs, a cover URL, a
    catalog token, a user ID, a status) is refused (422), so an unverified
    entry can never carry a provider ID or a cover. The ISBN is kept on the
    user's entry only, normalised to ISBN-13, and never written to the
    shared ``editions`` table."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1, max_length=TITLE_MAX)
    author: str = Field(..., min_length=1, max_length=AUTHOR_MAX)
    # ISBN-10 or ISBN-13, hyphens and spaces allowed (17 characters for a
    # hyphenated ISBN-13; a little slack for spaces).
    isbn: str | None = Field(None, max_length=20)

    @field_validator("title", "author")
    @classmethod
    def text_must_be_clean(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        if _CONTROL.search(v):
            raise ValueError("must not contain control characters")
        return v

    @field_validator("isbn")
    @classmethod
    def isbn_must_be_valid(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        return normalise_user_isbn(v)


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


# The UserBookRead fields that are not read off the library row (#616).
_UNVERIFIED_FIELDS = ("unverified", "unverified_isbn_13")


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
    # True for the user's own unverified entry (#616): ``book`` then carries
    # the entry's id, title and author only (no provider IDs, cover or
    # editions) and ``unverified_isbn_13`` the ISBN the user typed, if any.
    unverified: bool = False
    unverified_isbn_13: str | None = None

    model_config = {"from_attributes": True}

    @model_validator(mode="before")
    @classmethod
    def _unverified_entry(cls, data: object) -> object:
        """Serialise a library row (#616). An unverified entry (a row whose
        ``custom_book_id`` is set) gets its private ``user_custom_books`` row
        as ``book`` and ``unverified: true``; a catalog entry is read field by
        field with ``unverified: false``, never from attributes of that name.
        ``custom_book_id`` is read first, so ``custom_book`` is touched only
        when it is needed (and was loaded)."""
        if isinstance(data, dict):
            return data
        # Every field the row supplies; the two unverified fields are set below.
        row_fields = [
            name for name in cls.model_fields if name not in _UNVERIFIED_FIELDS
        ]
        if getattr(data, "custom_book_id", None) is None:
            return {
                **{name: getattr(data, name, None) for name in row_fields},
                "unverified": False,
                "unverified_isbn_13": None,
            }
        custom = data.custom_book  # type: ignore[attr-defined]
        return {
            **{name: getattr(data, name, None) for name in row_fields},
            "book": {
                "id": custom.id,
                "title": custom.title,
                "author": custom.author,
                # Not a catalog book: no language was ever read for it.
                "language": None,
                "created_at": custom.created_at,
                "updated_at": custom.updated_at,
            },
            "edition": None,
            "unverified": True,
            "unverified_isbn_13": custom.isbn_13,
        }

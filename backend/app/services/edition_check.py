"""One check for every write that links an edition to a book (#686).

A library entry must never point at an edition of a different book: that
corrupts the user's format / ISBN / cover and breaks duplicate detection by
edition. Every write path that takes an ``edition_id`` calls
``ensure_edition_belongs_to_book`` (or ``require_edition_of_book`` in a
router) before writing, so the rule lives in one place
(``user_book_formats``, #501, reuses it).
"""

import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.edition import Edition

# Stable 422 detail: the edition is not an edition of the book.
EDITION_BOOK_MISMATCH = "edition_book_mismatch"


class EditionBookMismatchError(Exception):
    """The edition does not exist or belongs to another book."""


async def ensure_edition_belongs_to_book(
    db: AsyncSession, book_id: uuid.UUID, edition_id: uuid.UUID | None
) -> None:
    """Raise ``EditionBookMismatchError`` unless ``edition_id`` is ``None`` or
    names an edition of ``book_id``. An unknown edition is refused the same
    way, so the answer never says whether another book's edition exists."""
    if edition_id is None:
        return
    result = await db.execute(
        select(Edition.id).where(Edition.id == edition_id, Edition.book_id == book_id)
    )
    if result.scalar_one_or_none() is None:
        raise EditionBookMismatchError


async def require_edition_of_book(
    db: AsyncSession, book_id: uuid.UUID, edition_id: uuid.UUID | None
) -> None:
    """``ensure_edition_belongs_to_book`` as an HTTP 422 for routers."""
    try:
        await ensure_edition_belongs_to_book(db, book_id, edition_id)
    except EditionBookMismatchError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=EDITION_BOOK_MISMATCH,
        ) from None

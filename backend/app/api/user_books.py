"""User books endpoints — wishlist, purchased, status updates."""

import logging
import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.ai import scan_corrections
from app.auth.dependencies import get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.core.limiter import limiter
from app.models.book import Book
from app.models.edition import Edition
from app.models.user import User
from app.models.user_book import UserBook
from app.schemas.user_book import (
    PurchasedCreate,
    UnverifiedBookCreate,
    UserBookRead,
    UserBookUpdate,
    WishlistRequest,
)
from app.services.edition_check import require_edition_of_book
from app.services.unverified_books import (
    UnverifiedBookService,
    UnverifiedDuplicateError,
    UnverifiedLimitError,
)
from app.services.wishlist import (
    AlreadyOwnedError,
    BookNotInCatalogError,
    CatalogTokenInvalidError,
    WishlistService,
)

# 422 detail when POST /wishlist would need a new catalog row but the book
# carries no valid catalog token (#602).
CATALOG_TOKEN_INVALID = "catalog_token_invalid"
# 422 detail when the book has no provider ID (no Open Library work ID and no
# Google Books ID): it is not in the catalog and can never be added (#602).
BOOK_NOT_IN_CATALOG = "book_not_in_catalog"
# 409 details from POST /wishlist/unverified (#616): the user already has an
# unverified entry with this title and author or ISBN, or holds the maximum
# number of unverified entries (settings.unverified_books_per_user_max).
UNVERIFIED_DUPLICATE = "unverified_duplicate"
UNVERIFIED_LIMIT_REACHED = "unverified_limit_reached"

logger = logging.getLogger(__name__)

router = APIRouter(tags=["user-books"])

BookStatus = Literal["wishlisted", "purchased", "reading", "read"]


def _user_book_query(user_id: uuid.UUID):
    return (
        select(UserBook)
        .where(UserBook.user_id == user_id)
        .options(
            selectinload(UserBook.book).selectinload(Book.editions),
            selectinload(UserBook.edition),
            # The user's own unverified entry (#616); only ever loaded
            # through this user's library row.
            selectinload(UserBook.custom_book),
        )
        .order_by(UserBook.created_at.desc())
    )


@router.post(
    "/wishlist", response_model=UserBookRead, status_code=status.HTTP_201_CREATED
)
@limiter.limit(settings.rate_limit_writes)
async def add_to_wishlist(
    request: Request,
    req: WishlistRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserBook:
    service = WishlistService()
    try:
        return await service.add(db, current_user.id, req)
    except AlreadyOwnedError as exc:
        # The user already has this book (#587): nothing was written. The
        # detail is the ownership code: owned_same_edition,
        # owned_other_edition or owned_edition_unknown.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=exc.ownership
        ) from None
    except BookNotInCatalogError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=BOOK_NOT_IN_CATALOG,
        ) from None
    except CatalogTokenInvalidError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=CATALOG_TOKEN_INVALID,
        ) from None


@router.post(
    "/wishlist/unverified",
    response_model=UserBookRead,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(settings.rate_limit_writes)
async def add_unverified_to_wishlist(
    request: Request,
    req: UnverifiedBookCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserBookRead:
    """Add a book the catalog cannot find as the user's own, private,
    unverified entry (#616). Never touches ``books`` / ``editions``.

    With ``scan_reference`` (#631), once the entry is saved, the server
    records whether the title, author and ISBN differ from the scan
    candidate it names (``app.ai.scan_corrections``). The response is built
    before that and never depends on it: a reference to nothing usable
    (another user's record, an expired one, a bad index) is ignored, and a
    failure there never fails the save."""
    user_id = current_user.id
    try:
        entry = await UnverifiedBookService().add(db, user_id, req)
    except UnverifiedDuplicateError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=UNVERIFIED_DUPLICATE
        ) from None
    except UnverifiedLimitError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=UNVERIFIED_LIMIT_REACHED
        ) from None
    # Serialised now, so nothing the correction step does (a rollback that
    # expires the row) can change or break the response.
    body = UserBookRead.model_validate(entry)
    ref = req.scan_reference
    if ref is not None:
        await scan_corrections.record_quietly(
            db,
            user_id=user_id,
            scan_record_id=ref.scan_record_id,
            candidate_index=ref.candidate_index,
            title=req.title,
            author=req.author,
            isbn=req.isbn,
        )
    return body


@router.get("/user-books", response_model=list[UserBookRead])
@limiter.limit(settings.rate_limit_reads)
async def list_user_books(
    request: Request,
    status_filter: BookStatus | None = Query(default=None, alias="status"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[UserBook]:
    q = _user_book_query(current_user.id)
    if status_filter:
        q = q.where(UserBook.status == status_filter)
    result = await db.execute(q)
    return list(result.scalars().all())


@router.post(
    "/purchased", response_model=UserBookRead, status_code=status.HTTP_201_CREATED
)
@limiter.limit(settings.rate_limit_writes)
async def add_purchased(
    request: Request,
    req: PurchasedCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserBook:
    # Verify book exists
    book_result = await db.execute(select(Book).where(Book.id == req.book_id))
    book = book_result.scalar_one_or_none()
    if not book:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Book not found"
        )

    # A client-sent edition must be an edition of this book (#686).
    await require_edition_of_book(db, req.book_id, req.edition_id)

    # Resolve edition if ISBN provided
    edition_id: uuid.UUID | None = req.edition_id
    if req.isbn_13 and not edition_id:
        ed_result = await db.execute(
            select(Edition).where(
                Edition.book_id == req.book_id,
                Edition.isbn_13 == req.isbn_13,
            )
        )
        edition = ed_result.scalar_one_or_none()
        if edition:
            edition_id = edition.id

    # Find existing user_book or create
    ub_result = await db.execute(
        select(UserBook).where(
            UserBook.user_id == current_user.id,
            UserBook.book_id == req.book_id,
        )
    )
    user_book = ub_result.scalar_one_or_none()
    now = datetime.now(UTC)

    if user_book:
        user_book.status = "purchased"
        user_book.purchased_at = now
        if edition_id:
            user_book.edition_id = edition_id
    else:
        user_book = UserBook(
            user_id=current_user.id,
            book_id=req.book_id,
            edition_id=edition_id,
            status="purchased",
            purchased_at=now,
        )
        db.add(user_book)

    await db.commit()
    await db.refresh(user_book)

    result = await db.execute(
        _user_book_query(current_user.id).where(UserBook.id == user_book.id)
    )
    return result.scalar_one()


@router.patch("/user-books/{user_book_id}", response_model=UserBookRead)
@limiter.limit(settings.rate_limit_writes)
async def update_user_book(
    request: Request,
    user_book_id: uuid.UUID,
    req: UserBookUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserBook:
    result = await db.execute(
        _user_book_query(current_user.id).where(UserBook.id == user_book_id)
    )
    user_book = result.scalar_one_or_none()
    if not user_book:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    now = datetime.now(UTC)

    if req.status and req.status != user_book.status:
        user_book.status = req.status
        if req.status == "purchased" and not user_book.purchased_at:
            user_book.purchased_at = now
        elif req.status == "reading" and not user_book.started_at:
            user_book.started_at = now
        elif req.status == "read" and not user_book.finished_at:
            user_book.finished_at = now

    if req.notes is not None:
        user_book.notes = req.notes
    if req.rating is not None:
        user_book.rating = req.rating

    await db.commit()
    await db.refresh(user_book)

    result = await db.execute(
        _user_book_query(current_user.id).where(UserBook.id == user_book.id)
    )
    return result.scalar_one()


@router.delete("/user-books/{user_book_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit(settings.rate_limit_writes)
async def delete_user_book(
    request: Request,
    user_book_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(
        select(UserBook)
        .where(
            UserBook.id == user_book_id,
            UserBook.user_id == current_user.id,
        )
        .options(selectinload(UserBook.custom_book))
    )
    user_book = result.scalar_one_or_none()
    if not user_book:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    custom_book = user_book.custom_book
    await db.delete(user_book)
    if custom_book is not None:
        # An unverified entry (#616) exists only for this library row:
        # removing the book removes it too, so it no longer counts towards
        # the user's cap.
        await db.delete(custom_book)
    await db.commit()

"""User books endpoints — wishlist, purchased, status updates, trash.

Deleting an entry moves it to the trash (``deleted_at``, #683). Every query
here reads live entries only (``UserBook.live()``, via ``_user_book_query``)
except the trash list, restore and the daily purge
(``app/services/trash.py``)."""

import logging
import uuid
from datetime import UTC, datetime
from typing import Literal, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
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
    TrashedUserBookRead,
    UnverifiedBookCreate,
    UserBookRead,
    UserBookUpdate,
    WishlistRequest,
)
from app.services import catalog
from app.services.edition_check import require_edition_of_book
from app.services.trash import evict_over_cap, evict_same_book
from app.services.unverified_books import (
    UnverifiedBookService,
    UnverifiedDuplicateError,
    UnverifiedLimitError,
    lock_user,
    refuse_if_cannot_hold,
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
# 422 detail when PATCH /user-books/{id} sends title / author / cover
# overrides (#682) for an unverified entry (#616).
OVERRIDES_NOT_SUPPORTED = "overrides_not_supported"
# 409 detail when POST /user-books/{id}/restore (#683) would give the user a
# second live entry of a book: a live entry of the same work (any row of a
# merged work, #682), or for an unverified entry the same title and author
# or ISBN. A restore that would exceed the unverified cap answers
# UNVERIFIED_LIMIT_REACHED, as an add does.
RESTORE_CONFLICT = "restore_conflict"

logger = logging.getLogger(__name__)

router = APIRouter(tags=["user-books"])

BookStatus = Literal["wishlisted", "purchased", "reading", "read"]


def _user_book_query(user_id: uuid.UUID, *, trashed: bool = False):
    """The user's library entries, loaded for ``UserBookRead``. Live entries
    (newest first) by default; with ``trashed`` the entries in the trash
    instead (#683), most recently deleted first. Every library read goes
    through here, so an entry in the trash is never read as a live one."""
    if trashed:
        state = UserBook.deleted_at.is_not(None)
        # Ties by id, highest first: the order the trash eviction keeps
        # (``trash.evict_over_cap``), so the list shows exactly those rows.
        order = (UserBook.deleted_at.desc(), UserBook.id.desc())
    else:
        state = UserBook.live()
        order = (UserBook.created_at.desc(),)
    return (
        select(UserBook)
        .where(UserBook.user_id == user_id, state)
        .options(
            selectinload(UserBook.book).selectinload(Book.editions),
            # A merged catalog row is read as its survivor (#682).
            selectinload(UserBook.book)
            .selectinload(Book.merged_into)
            .selectinload(Book.editions),
            selectinload(UserBook.edition),
            # The user's own unverified entry (#616); only ever loaded
            # through this user's library row.
            selectinload(UserBook.custom_book),
        )
        .order_by(*order)
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
    # Serialise with this user's deletes, restores and other adds (#683),
    # before the service's first read: its ownership check must not race a
    # restore of an entry on a merged row of the same work, which the unique
    # index (per book row) cannot catch. Lock order: users first, then
    # books / editions / user_books. Taken here, the only caller of
    # WishlistService.add, in the same transaction.
    await lock_user(db, current_user.id)
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
    # Serialise with this user's deletes, restores and adds (#683): the
    # existing-entry lookup below must not race a DELETE or a restore.
    # Lock order: users, then user_books.
    await lock_user(db, current_user.id)
    # Verify book exists
    book_result = await db.execute(select(Book).where(Book.id == req.book_id))
    book = book_result.scalar_one_or_none()
    if not book:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Book not found"
        )
    # A merged catalog row is never the target of a new entry (#682): a
    # stale book_id adds (or promotes) the surviving row instead.
    survivor = await catalog.resolve_survivor(db, book)
    book_id = survivor.id

    # A client-sent edition must be an edition of this book (#686): of the
    # survivor, since a merge moves the merged row's editions to it (#682).
    await require_edition_of_book(db, book_id, req.edition_id)

    # Resolve edition if ISBN provided
    edition_id: uuid.UUID | None = req.edition_id
    if req.isbn_13 and not edition_id:
        ed_result = await db.execute(
            select(Edition).where(
                Edition.book_id == book_id,
                Edition.isbn_13 == req.isbn_13,
            )
        )
        edition = ed_result.scalar_one_or_none()
        if edition:
            edition_id = edition.id

    # Find existing user_book or create. An entry on a row merged into the
    # survivor is an entry of the same work (#682): promote it rather than
    # add a second one. The survivor's own entry wins, then the oldest.
    merged_ids = select(Book.id).where(Book.merged_into_id == book_id)
    ub_result = await db.execute(
        select(UserBook)
        .where(
            UserBook.user_id == current_user.id,
            # An entry in the trash (#683) is not promoted: the book is
            # added as a new live entry and the trashed one stays there.
            UserBook.live(),
            or_(UserBook.book_id == book_id, UserBook.book_id.in_(merged_ids)),
        )
        .order_by(UserBook.book_id != book_id, UserBook.created_at, UserBook.id)
        .limit(1)
        # Locked and re-checked as live, so a row trashed meanwhile is never
        # promoted (#683).
        .with_for_update(of=UserBook)
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
            book_id=book_id,
            edition_id=edition_id,
            status="purchased",
            purchased_at=now,
        )
        db.add(user_book)

    return await _reread_and_commit(db, current_user.id, user_book)


async def _reread_and_commit(
    db: AsyncSession, user_id: uuid.UUID, user_book: UserBook
) -> UserBook:
    """Flush, read the entry back for the response inside the same
    transaction (so a DELETE committed right after cannot turn the re-read
    into a 500, #683), then commit."""
    await db.flush()  # assigns a new row's id
    result = await db.execute(
        _user_book_query(user_id)
        .where(UserBook.id == user_book.id)
        .execution_options(populate_existing=True)
    )
    user_book = result.scalar_one()
    await db.commit()
    return user_book


@router.patch("/user-books/{user_book_id}", response_model=UserBookRead)
@limiter.limit(settings.rate_limit_writes)
async def update_user_book(
    request: Request,
    user_book_id: uuid.UUID,
    req: UserBookUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserBook:
    # Row-locked and live only: a PATCH racing a DELETE waits for it and
    # then finds the entry trashed (404), never writing to a trashed row.
    result = await db.execute(
        _user_book_query(current_user.id)
        .where(UserBook.id == user_book_id)
        .with_for_update(of=UserBook)
    )
    user_book = result.scalar_one_or_none()
    if not user_book:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    # The user's own title / author / cover (#682): this entry only, never
    # the shared catalog row. Checked first, so a refusal changes nothing.
    _apply_overrides(user_book, req)

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

    return await _reread_and_commit(db, current_user.id, user_book)


def _apply_overrides(user_book: UserBook, req: UserBookUpdate) -> None:
    """Set or clear the override columns ``req`` sent, on ``user_book``
    only (#682). An unverified entry (#616) is the user's own data and never
    carries a cover, so it takes no overrides: 422 ``overrides_not_supported``
    (nothing written)."""
    changes = req.override_changes()
    if not changes:
        return
    if user_book.custom_book_id is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=OVERRIDES_NOT_SUPPORTED,
        )
    for column, value in changes.items():
        setattr(user_book, column, value)


@router.get("/user-books/trash", response_model=list[TrashedUserBookRead])
@limiter.limit(settings.rate_limit_reads)
async def list_trash(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[UserBook]:
    """The user's entries in the trash (#683), most recently deleted first,
    each with ``deleted_at`` and ``purge_after``. An entry stays here (and
    restorable) until the daily cleanup purges it, which can be up to a day
    after ``purge_after``. Deleting a book that already has a trashed copy
    replaces older trashed copies that hold no data of the user's (no
    notes, rating, edition, overrides, status beyond wishlisted); copies
    with the user's data stay. At most
    ``settings.trash_max_entries_per_user`` entries: the trash keeps 30 days
    or the newest N, whichever comes first."""
    # DELETE keeps the trash within the cap; the limit is a second bound.
    result = await db.execute(
        _user_book_query(current_user.id, trashed=True).limit(
            settings.trash_max_entries_per_user
        )
    )
    return list(result.scalars().all())


@router.delete("/user-books/{user_book_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit(settings.rate_limit_writes)
async def delete_user_book(
    request: Request,
    user_book_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Move the entry to the trash (#683): ``deleted_at`` is set on the
    library row and, for an unverified entry (#616), on its
    ``user_custom_books`` row. Nothing is deleted; the entry disappears from
    every library read and check, and ``POST /user-books/{id}/restore``
    brings it back until the daily cleanup purges it. An entry already in
    the trash is a 404, like one that does not exist. In the same
    transaction, the user's other, older trashed entries of the same book
    that hold no data of the user's are hard-deleted
    (``trash.evict_same_book``, so an add / delete loop never grows the
    trash; a copy with notes, a rating, an edition, overrides or a status
    past wishlisted is kept), then the oldest trashed entries beyond
    ``settings.trash_max_entries_per_user`` (``trash.evict_over_cap``, a
    backstop)."""
    # Lock order users -> user_books -> user_custom_books. The user lock
    # serialises this with the user's other deletes, restores and adds; the
    # row lock (live only) makes a concurrent second DELETE a 404.
    await lock_user(db, current_user.id)
    result = await db.execute(
        select(UserBook)
        .where(
            UserBook.id == user_book_id,
            UserBook.user_id == current_user.id,
            UserBook.live(),
        )
        # The book (catalog entry) or custom book (unverified entry) names
        # the book whose older trashed copies are evicted.
        .options(selectinload(UserBook.book), selectinload(UserBook.custom_book))
        .with_for_update(of=UserBook)
    )
    user_book = result.scalar_one_or_none()
    if not user_book:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    now = datetime.now(UTC)
    user_book.deleted_at = now
    if user_book.custom_book is not None:
        # An unverified entry exists only for this library row: it goes to
        # the trash with it, and so no longer counts towards the cap.
        user_book.custom_book.deleted_at = now
    await evict_same_book(db, current_user.id, user_book)
    await evict_over_cap(db, current_user.id)
    await db.commit()


@router.post("/user-books/{user_book_id}/restore", response_model=UserBookRead)
@limiter.limit(settings.rate_limit_writes)
async def restore_user_book(
    request: Request,
    user_book_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserBook:
    """Bring an entry back from the trash (#683), exactly as it was (status,
    dates, notes, rating, edition, overrides). 404 when the entry is not
    this user's or not in the trash. 409 ``restore_conflict`` when the user
    has a live entry of the same book (any row of a merged work, #682, or
    for an unverified entry the same title and author or ISBN); 409
    ``unverified_limit_reached`` when restoring an unverified entry would
    exceed ``settings.unverified_books_per_user_max``. Nothing is written on
    a refusal. An entry past ``purge_after`` that the cleanup has not purged
    yet can still be restored."""
    user_id = current_user.id
    # Serialise with this user's other restores and unverified adds: the
    # checks below read the user's live entries.
    await lock_user(db, user_id)
    # Row-locked, so a restore racing the purge waits for it and then finds
    # the row gone (404) rather than updating a deleted row.
    result = await db.execute(
        _user_book_query(user_id, trashed=True)
        .where(UserBook.id == user_book_id)
        .with_for_update(of=UserBook)
    )
    user_book = result.scalar_one_or_none()
    if not user_book:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    custom = user_book.custom_book
    if custom is not None:
        try:
            await refuse_if_cannot_hold(
                db, user_id, custom.title, custom.author, custom.isbn_13
            )
        except UnverifiedDuplicateError:
            await _refuse_restore(db, RESTORE_CONFLICT)
        except UnverifiedLimitError:
            await _refuse_restore(db, UNVERIFIED_LIMIT_REACHED)
    elif await _has_live_entry_of_work(db, user_id, user_book.book):
        await _refuse_restore(db, RESTORE_CONFLICT)

    user_book.deleted_at = None
    if custom is not None:
        custom.deleted_at = None
    try:
        return await _reread_and_commit(db, user_id, user_book)
    except IntegrityError:
        # A live entry of the same book was added meanwhile
        # (uq_user_books_user_book_live).
        await _refuse_restore(db, RESTORE_CONFLICT)


async def _has_live_entry_of_work(
    db: AsyncSession, user_id: uuid.UUID, book: Book
) -> bool:
    """Whether the user has a live entry on ``book``'s work: its survivor
    (``book`` itself unless merged, #682) or any row merged into it."""
    result = await db.execute(
        select(UserBook.id)
        .where(
            UserBook.user_id == user_id,
            UserBook.live(),
            catalog.of_work(UserBook.book_id, book),
        )
        .limit(1)
    )
    return bool(result.all())


async def _refuse_restore(db: AsyncSession, detail: str) -> NoReturn:
    await db.rollback()
    logger.info("restore: refused (%s)", detail)
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)

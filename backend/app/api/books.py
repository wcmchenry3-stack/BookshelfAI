"""Book search endpoint — free-text query returning enriched candidates."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.core import catalog_token
from app.core.config import settings
from app.core.database import get_db
from app.core.limiter import limiter
from app.models.user import User
from app.schemas.book import NOT_OWNED, EnrichedBook
from app.services.book_identifier import BookCandidate
from app.services.deduplication import DeduplicationService
from app.services.enrichment import EnrichmentService
from app.services.google_books import GoogleBooksService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/books", tags=["books"])

_gb = GoogleBooksService()
_enrichment = EnrichmentService()


def _volume_to_candidate(volume: dict) -> BookCandidate:
    info = volume.get("volumeInfo", {})
    title = info.get("title", "Unknown")
    authors = info.get("authors", [])
    author = authors[0] if authors else "Unknown"

    isbn_13 = isbn_10 = None
    for id_entry in info.get("industryIdentifiers", []):
        if id_entry.get("type") == "ISBN_13":
            isbn_13 = id_entry.get("identifier")
        elif id_entry.get("type") == "ISBN_10":
            isbn_10 = id_entry.get("identifier")

    return BookCandidate(
        title=title,
        author=author,
        confidence=0.9,
        isbn_13=isbn_13,
        isbn_10=isbn_10,
    )


@router.get("/search", response_model=list[EnrichedBook])
@limiter.limit(settings.rate_limit_books_search)
async def search_books(
    request: Request,
    q: str = Query(
        ...,
        min_length=2,
        max_length=256,
        description="Free-text query, e.g. 'John Adams McCullough'",
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[EnrichedBook]:
    """Search for books by free text. Returns up to 3 enriched candidates,
    each signed for this user (``catalog_token``, #602) and marked with
    whether they already have it (``ownership``, #587)."""
    try:
        volumes = await _gb.search_query(q, limit=3)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Book search unavailable") from exc

    if not volumes:
        return []

    candidates = [_volume_to_candidate(v) for v in volumes]
    books = await _enrichment.enrich(candidates)
    books = await _mark_library(db, current_user, books)
    return catalog_token.sign_books(books, current_user.id)


async def _mark_library(
    db: AsyncSession, user: User, books: list[EnrichedBook]
) -> list[EnrichedBook]:
    """The edition-level library check (#587). A failed check never fails
    the search: the results go out unmarked, and POST /wishlist still
    refuses a book the user already has (409)."""
    if not books:
        return books
    try:
        return await DeduplicationService().check(db, str(user.id), books)
    except Exception as exc:  # noqa: BLE001 - the search must still answer
        logger.warning("books/search: library check failed (%s)", type(exc).__name__)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            logger.warning("books/search: rollback after the library check failed")
        for book in books:
            book.ownership = NOT_OWNED
            book.already_in_library = False
        return books

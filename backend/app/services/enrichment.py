"""EnrichmentService — merges Open Library + Google Books into EnrichedBook."""

import asyncio
import logging

from app.schemas.book import EditionPreview, EnrichedBook
from app.services.book_identifier import BookCandidate
from app.services.google_books import GoogleBooksService
from app.services.open_library import OpenLibraryService

logger = logging.getLogger(__name__)


# Each candidate fires one Open Library and one Google Books request. A shelf
# photo can yield 15 candidates, so cap how many are in flight at once.
MAX_CONCURRENT_ENRICHMENTS = 5


class EnrichmentService:
    def __init__(self) -> None:
        self.ol = OpenLibraryService()
        self.gb = GoogleBooksService()

    async def enrich(
        self, candidates: list[BookCandidate], limit: int = 3
    ) -> list[EnrichedBook]:
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_ENRICHMENTS)

        async def _bounded(candidate: BookCandidate) -> EnrichedBook:
            async with semaphore:
                return await self._enrich_one(candidate)

        tasks = [_bounded(c) for c in candidates[:limit]]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        enriched = []
        for r in results:
            if isinstance(r, EnrichedBook):
                enriched.append(r)
            else:
                logger.warning("Enrichment failed for a candidate: %s", r)
        return enriched

    async def _enrich_one(self, candidate: BookCandidate) -> EnrichedBook:
        # Fetch from both APIs concurrently
        ol_task = (
            self.ol.search_by_isbn(candidate.isbn_13)
            if candidate.isbn_13
            else self.ol.search(candidate.title, candidate.author)
        )
        gb_task = (
            self.gb.search_by_isbn(candidate.isbn_13)
            if candidate.isbn_13
            else self.gb.search(candidate.title, candidate.author)
        )
        ol_doc, gb_volume = await asyncio.gather(
            ol_task, gb_task, return_exceptions=True
        )

        if isinstance(ol_doc, Exception):
            logger.warning("Open Library error: %s", ol_doc)
            ol_doc = None
        if isinstance(gb_volume, Exception):
            logger.warning("Google Books error: %s", gb_volume)
            gb_volume = None

        work_id = self.ol.extract_work_id(ol_doc) if ol_doc else None
        gb_info = self.gb.extract_info(gb_volume) if gb_volume else {}

        subjects = (ol_doc or {}).get("subject", [])[:10] if ol_doc else []
        authors = (ol_doc or {}).get("author_name", [candidate.author])

        edition = EditionPreview(
            isbn_13=candidate.isbn_13,
            isbn_10=candidate.isbn_10,
            publisher=gb_info.get("publisher"),
            publish_year=(
                int(gb_info["publish_year"]) if gb_info.get("publish_year") else None
            ),
            page_count=gb_info.get("page_count"),
            format="unknown",
        )

        return EnrichedBook(
            open_library_work_id=work_id,
            google_books_id=gb_info.get("google_books_id"),
            title=candidate.title,
            author=authors[0] if authors else candidate.author,
            description=gb_info.get("description"),
            cover_url=gb_info.get("cover_url"),
            subjects=subjects,
            confidence=candidate.confidence,
            already_in_library=False,  # set by DeduplicationService
            editions=[edition],
        )

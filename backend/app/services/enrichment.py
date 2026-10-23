"""EnrichmentService — merges Open Library + Google Books into EnrichedBook.

The candidate (the vision model's reading of the photo, or a search hit) is
only a search key. Whenever a provider matched, every catalog field of the
result comes from the provider records (#602), because a signed result may
create shared ``books`` / ``editions`` rows:

- title and author: the Open Library doc (``title``, ``author_name[0]``),
  otherwise the Google Books volume (``volumeInfo.title``, ``authors[0]``);
- edition ISBNs: the candidate's ISBN-13 / ISBN-10 only when a provider
  confirmed it (the ISBN-13 that was looked up and matched, or an ISBN the
  provider record lists), else none.

The candidate's own title and author are shown only when nothing matched;
such a result has no provider ID, is never signed and so can never create a
catalog row.
"""

import asyncio
import contextvars
import logging

from app.schemas.book import EditionPreview, EnrichedBook
from app.services.book_identifier import BookCandidate
from app.services.google_books import GoogleBooksService
from app.services.open_library import OpenLibraryService

logger = logging.getLogger(__name__)

# Shown when a provider matched but neither record names an author.
UNKNOWN_AUTHOR = "Unknown"
# EnrichedBook's limit for title / author.
_MAX_TEXT = 500


# Each candidate fires one Open Library and one Google Books request. A shelf
# photo can yield 15 candidates, so cap how many are in flight at once.
MAX_CONCURRENT_ENRICHMENTS = 5

# Lookup failures (an Open Library or Google Books call that raised) during
# the current ``enrich`` call. A context variable, so concurrent calls on a
# shared instance never see each other's failures; the gathered tasks copy
# the context and so append to the same list.
_LOOKUP_FAILURES: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "enrichment_lookup_failures", default=None
)


def _provider_text(value: object) -> str | None:
    """A usable provider string (non-empty, within the schema limit)."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if 0 < len(value) <= _MAX_TEXT else None


def _first_text(values: object) -> str | None:
    if isinstance(values, list) and values:
        return _provider_text(values[0])
    return None


def _gb_isbns(volume: dict | None) -> set[str]:
    info = (volume or {}).get("volumeInfo") or {}
    identifiers = info.get("industryIdentifiers") or []
    return {
        entry["identifier"]
        for entry in identifiers
        if isinstance(entry, dict) and isinstance(entry.get("identifier"), str)
    }


def _ol_isbns(doc: dict | None) -> set[str]:
    values = (doc or {}).get("isbn") or []
    return (
        {v for v in values if isinstance(v, str)} if isinstance(values, list) else set()
    )


def _note_failure(source: str) -> None:
    failures = _LOOKUP_FAILURES.get()
    if failures is not None:
        failures.append(source)


class EnrichmentService:
    def __init__(self) -> None:
        self.ol = OpenLibraryService()
        self.gb = GoogleBooksService()
        # After ``enrich``: True when every lookup for every candidate
        # finished (a lookup that found nothing still finished), False when
        # any Open Library / Google Books call or any candidate failed. Scan
        # records (#585) only save an enrichment that finished. Meant for a
        # per-request instance; on a shared one it is the last call's.
        self.last_complete: bool | None = None

    async def enrich(
        self, candidates: list[BookCandidate], limit: int = 3
    ) -> list[EnrichedBook]:
        self.last_complete = False
        failures: list[str] = []
        token = _LOOKUP_FAILURES.set(failures)
        try:
            enriched = await self._enrich_all(candidates, limit)
        finally:
            _LOOKUP_FAILURES.reset(token)
        self.last_complete = not failures and len(enriched) == len(candidates[:limit])
        return enriched

    async def _enrich_all(
        self, candidates: list[BookCandidate], limit: int
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
            _note_failure("open_library")
            ol_doc = None
        if isinstance(gb_volume, Exception):
            logger.warning("Google Books error: %s", gb_volume)
            _note_failure("google_books")
            gb_volume = None

        # A record without a usable title is not a match: nothing from it
        # (ID, cover, subjects) may be paired with the candidate's reading.
        ol_title = _provider_text(ol_doc.get("title")) if ol_doc else None
        if not ol_title:
            ol_doc = None
        gb_title = (
            _provider_text((gb_volume.get("volumeInfo") or {}).get("title"))
            if gb_volume
            else None
        )
        if not gb_title:
            gb_volume = None

        work_id = self.ol.extract_work_id(ol_doc) if ol_doc else None
        gb_info = self.gb.extract_info(gb_volume) if gb_volume else {}

        subjects = (ol_doc or {}).get("subject", [])[:10] if ol_doc else []

        if ol_doc or gb_volume:
            title = ol_title or gb_title
            author = (
                (_first_text(ol_doc.get("author_name")) if ol_doc else None)
                or (
                    _first_text((gb_volume.get("volumeInfo") or {}).get("authors"))
                    if gb_volume
                    else None
                )
                or UNKNOWN_AUTHOR
            )
        else:
            title, author = candidate.title, candidate.author

        # ISBNs a provider confirmed: the one looked up when a lookup by it
        # matched, and any the matched records list.
        confirmed = _ol_isbns(ol_doc) | _gb_isbns(gb_volume)
        if candidate.isbn_13 and (ol_doc or gb_volume):
            confirmed.add(candidate.isbn_13)

        edition = EditionPreview(
            isbn_13=candidate.isbn_13 if candidate.isbn_13 in confirmed else None,
            isbn_10=candidate.isbn_10 if candidate.isbn_10 in confirmed else None,
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
            title=title,
            author=author,
            description=gb_info.get("description"),
            cover_url=gb_info.get("cover_url"),
            subjects=subjects,
            confidence=candidate.confidence,
            already_in_library=False,  # set by DeduplicationService
            editions=[edition],
        )

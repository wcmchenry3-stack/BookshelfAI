"""EnrichmentService — merges Open Library + Google Books into EnrichedBook.

The candidate (the vision model's reading of the photo, or a search hit) is
only a search key. Whenever a provider matched, every catalog field of the
result comes from the provider records (#602), because a signed result may
create shared ``books`` / ``editions`` rows:

- title and author: the Open Library doc (``title``, ``author_name[0]``),
  otherwise the Google Books volume (``volumeInfo.title``, ``authors[0]``);
- edition ISBNs: the candidate's ISBN-13 only when a provider record used
  for the result lists it among its identifiers (Open Library ``isbn``,
  Google Books ``industryIdentifiers``); a lookup by that ISBN returning a
  record is not enough, since Google Books ``q=isbn:X`` is a keyword search.
  The ISBN-10 is kept only when it is the ISBN-10 form of that confirmed
  ISBN-13 (an Open Library work lists every edition's ISBNs, so a listed
  ISBN-10 may belong to another edition); without a confirmed ISBN-13, a
  listed ISBN-10 is kept on its own.

When both providers matched, the Google Books volume is used only if it is
the same book as the Open Library doc (:func:`records_agree`): a shared
ISBN, or the same normalised title and first-author surname. Otherwise it is
dropped entirely (no ``google_books_id``, description, cover or edition
data from it), so one book's Google Books ID is never wired to another
book's work.

The candidate's own title and author are shown only when nothing matched;
such a result has no provider ID, is never signed and so can never create a
catalog row.
"""

import asyncio
import contextvars
import logging
import re
import unicodedata

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


_NON_WORD = re.compile(r"[\W_]+")
_LEADING_ARTICLE = re.compile(r"^(?:the|a|an) ")


def _normalise(value: object) -> str:
    """Casefolded, punctuation stripped, whitespace collapsed."""
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(_NON_WORD.sub(" ", value).split())


def normalise_title(value: object) -> str:
    """A title for comparison: :func:`_normalise`, minus a leading article."""
    return _LEADING_ARTICLE.sub("", _normalise(value), count=1)


def _surname(authors: object) -> str:
    """The normalised last word of the first author's name."""
    if not isinstance(authors, list) or not authors:
        return ""
    words = _normalise(authors[0]).split()
    return words[-1] if words else ""


def records_agree(ol_doc: dict, gb_volume: dict) -> bool:
    """Whether an Open Library doc and a Google Books volume describe the
    same book: they share an ISBN, or their normalised titles and
    first-author surnames both match (and are non-empty)."""
    if _ol_isbns(ol_doc) & _gb_isbns(gb_volume):
        return True
    info = gb_volume.get("volumeInfo") or {}
    ol_title, gb_title = normalise_title(ol_doc.get("title")), normalise_title(
        info.get("title")
    )
    ol_author, gb_author = _surname(ol_doc.get("author_name")), _surname(
        info.get("authors")
    )
    return bool(ol_title and ol_author) and (ol_title, ol_author) == (
        gb_title,
        gb_author,
    )


def isbn_13_to_10(isbn_13: str | None) -> str | None:
    """The ISBN-10 form of a 978-prefixed ISBN-13, else None."""
    if not isbn_13 or len(isbn_13) != 13 or not isbn_13.isdigit():
        return None
    if not isbn_13.startswith("978"):
        return None
    body = isbn_13[3:12]
    total = sum((10 - i) * int(d) for i, d in enumerate(body))
    check = (11 - total % 11) % 11
    return body + ("X" if check == 10 else str(check))


def _edition_isbns(
    isbn_13: str | None, isbn_10: str | None, confirmed: set[str]
) -> tuple[str | None, str | None]:
    """The candidate's ISBNs a provider confirmed, paired consistently: an
    ISBN-10 beside a confirmed ISBN-13 only when it is that ISBN-13's
    ISBN-10 form; without one, a confirmed ISBN-10 on its own."""
    isbn_13 = isbn_13 if isbn_13 in confirmed else None
    if isbn_10 is not None:
        isbn_10 = isbn_10.upper()
    if isbn_13:
        return isbn_13, isbn_10 if isbn_10 == isbn_13_to_10(isbn_13) else None
    confirmed_upper = {c.upper() for c in confirmed}
    return None, isbn_10 if isbn_10 in confirmed_upper else None


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
        # Two different books (e.g. title searches that matched different
        # works): keep Open Library's, use nothing from the volume.
        if ol_doc and gb_volume and not records_agree(ol_doc, gb_volume):
            logger.info("enrichment: Google Books volume disagrees; dropped")
            gb_volume, gb_title = None, None

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

        # ISBNs a provider confirmed: those the records used list. A lookup
        # by an ISBN that returned a record does not confirm it.
        confirmed = _ol_isbns(ol_doc) | _gb_isbns(gb_volume)
        isbn_13, isbn_10 = _edition_isbns(
            candidate.isbn_13, candidate.isbn_10, confirmed
        )

        edition = EditionPreview(
            isbn_13=isbn_13,
            isbn_10=isbn_10,
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

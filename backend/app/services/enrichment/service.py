"""EnrichmentService: looks a candidate up in Open Library and Google Books."""

import asyncio
import contextvars
import dataclasses
import logging
from collections.abc import Awaitable, Callable

from app.schemas.book import EditionPreview, EnrichedBook
from app.services.book_identifier import BookCandidate
from app.services.enrichment.authors import _author_contradicted, reading_surname
from app.services.enrichment.isbn import (
    _edition_isbns,
    _gb_isbns,
    _isbn_digits,
    _ol_isbns,
    valid_isbn_13,
)
from app.services.enrichment.scoring import (
    _gb_parts,
    _ol_parts,
    _RecordParts,
    _scorer,
    records_agree,
)
from app.services.enrichment.text import _first_text, _provider_text
from app.services.enrichment.titles import _listed_same_book, _split_title
from app.services.google_books import GoogleBooksService
from app.services.open_library import OpenLibraryService

# Keep the pre-split logger name: log filters and Sentry tags key on it.
logger = logging.getLogger("app.services.enrichment")

# Shown when a provider matched but neither record names an author.
UNKNOWN_AUTHOR = "Unknown"


# Each candidate fires one Open Library and one Google Books request, and at
# most one more of each (#743): a title search when that provider's ISBN
# lookup found nothing or another book. A shelf photo can yield 15
# candidates, so cap how many are in flight at once.
MAX_CONCURRENT_ENRICHMENTS = 5
# How many records one title search considers (#743).
TITLE_SEARCH_RESULTS = 5
# Time limits (#743 review): one provider request may take at most
# PROVIDER_CALL_TIMEOUT_S (the HTTP client's own 10 s applies per phase, so
# a request could otherwise run longer). Each provider's ISBN lookup and
# title search run in sequence, and the two providers' chains concurrently,
# so a candidate takes at most 2 sequential requests' time: 2 x L for a
# per-request latency L, at most 16 s. Candidates run in
# waves = ceil(scan_max_books / MAX_CONCURRENT_ENRICHMENTS), 3 for 15, so a
# whole scan takes up to waves x 2 x L when every candidate needs its title
# search (waves x L when its ISBN record fits or it has no ISBN). That is
# within ENRICHMENT_DEADLINE_S for L up to about 3.3 s (6.6 s when one
# request per provider is enough), not for every candidate at the timeout.
# The whole ``enrich`` call therefore stops at ENRICHMENT_DEADLINE_S:
# candidates still running then come back as their unsigned reading and the
# enrichment counts as incomplete. With the vision call's 45 s
# (SCAN_TIMEOUT_SECONDS) a scan stays well under Cloudflare's 100 s origin
# timeout.
PROVIDER_CALL_TIMEOUT_S = 8.0
ENRICHMENT_DEADLINE_S = 20.0
# What ``_lookup`` returns for a call that raised.
_FAILED = object()

# Lookup failures (an Open Library or Google Books call that raised) during
# the current ``enrich`` call. A context variable, so concurrent calls on a
# shared instance never see each other's failures; the gathered tasks copy
# the context and so append to the same list.
_LOOKUP_FAILURES: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "enrichment_lookup_failures", default=None
)


def _note_failure(source: str) -> None:
    failures = _LOOKUP_FAILURES.get()
    if failures is not None:
        failures.append(source)


_SOURCE_NAMES = {"open_library": "Open Library", "google_books": "Google Books"}


async def _lookup(call: Awaitable[object], source: str) -> object:
    """The call's result, or ``_FAILED`` (logged and noted as a lookup
    failure) when it raised or took longer than ``PROVIDER_CALL_TIMEOUT_S``."""
    try:
        return await asyncio.wait_for(call, PROVIDER_CALL_TIMEOUT_S)
    except TimeoutError:
        logger.warning("%s error: timed out", _SOURCE_NAMES[source])
        _note_failure(source)
        return _FAILED
    except Exception as exc:  # noqa: BLE001 - a provider error never fails the scan
        logger.warning("%s error: %s", _SOURCE_NAMES[source], exc)
        _note_failure(source)
        return _FAILED


def _isbn_record(
    candidate: BookCandidate,
    found: object,
    score: Callable[[dict], int],
    parts: Callable[[object], _RecordParts],
    isbns: Callable[[dict], set[str]],
) -> dict | None:
    """The record an ISBN lookup found, when it may be used (#743). Either
    it fits the reading (``score``), or it lists the ISBN and has a usable
    title (read in the provider's own shape, ``parts``) and either the
    reading has no title, or the main titles are the same and the two do
    not name different volumes (:func:`_listed_same_book`: "Sapiens" and
    "Sapiens: A Brief History of Humankind", never "Saga: Book One" and
    "Saga: Book Two") and the record does not name another author.
    Otherwise None (the provider is then searched by title).

    A record that lists the ISBN but whose main title differs from the
    reading's (a translated or paraphrased reading, say) is deliberately not
    used: the model's ISBN is often invented, and an invented ISBN that
    happens to exist would otherwise attach another book to the reading."""
    if not isinstance(found, dict):
        return None
    if score(found) > 0:
        return found
    display, full, authors = parts(found)
    if display and candidate.isbn_13 in isbns(found):
        if not _split_title(candidate.title)[0]:
            return found
        if _listed_same_book(candidate.title, full) and not _author_contradicted(
            candidate, authors
        ):
            return found
    logger.info("enrichment: ISBN lookup found another book; ignored")
    return None


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

        chosen = candidates[:limit]
        tasks = [asyncio.ensure_future(_bounded(c)) for c in chosen]
        results: list[object] = []
        if tasks:
            try:
                _, pending = await asyncio.wait(tasks, timeout=ENRICHMENT_DEADLINE_S)
            except BaseException:
                # The request itself was cancelled: never leave lookups behind.
                for task in tasks:
                    task.cancel()
                raise
            if pending:
                # Past the deadline: stop what is still running and show
                # those candidates unmatched (no provider data, unsigned).
                logger.warning(
                    "enrichment: deadline reached; %d of %d candidates unmatched",
                    len(pending),
                    len(tasks),
                )
                _note_failure("deadline")
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            for candidate, task in zip(chosen, tasks, strict=True):
                if task in pending:
                    results.append(self._build(candidate, None, None))
                elif task.cancelled():
                    # A provider raised CancelledError while this request was
                    # not cancelled (a real cancel never reaches here): a
                    # lookup failure, not a failed scan.
                    logger.warning("enrichment: a candidate's lookup was cancelled")
                    _note_failure("cancelled")
                    results.append(self._build(candidate, None, None))
                elif task.exception() is not None:
                    results.append(task.exception())
                else:
                    results.append(task.result())
        enriched = []
        for index, r in enumerate(results):
            if isinstance(r, EnrichedBook):
                # Which candidate it came from (#631): a failed candidate is
                # dropped, so the position in this list is not enough.
                r.candidate_index = index
                enriched.append(r)
            else:
                logger.warning("Enrichment failed for a candidate: %s", r)
        return enriched

    async def _find_records(
        self, candidate: BookCandidate
    ) -> tuple[dict | None, dict | None]:
        """The Open Library doc and Google Books volume for a candidate, each
        None when that provider has no record that fits the reading (#743).

        Each provider runs its own chain, and the two chains run
        concurrently. With a valid ISBN-13 (an ISBN with a bad check digit,
        which the model often invents, is not looked up), the provider is
        first looked up by it; the record is used only when
        :func:`_isbn_record` allows it, since a misread or invented ISBN
        often names another book, or none. When that lookup finished without
        such a record, and for every candidate without a valid ISBN-13, the
        provider is searched by title and author surname
        (``TITLE_SEARCH_RESULTS`` records, the best fitting one kept). A
        reading without a title is never searched by title, and a lookup
        that raised is not retried. So a candidate costs at most two
        requests per provider, four in all, and takes at most two sequential
        requests' time."""
        isbn_13 = candidate.isbn_13
        if isbn_13 and not valid_isbn_13(isbn_13):
            logger.info("enrichment: ISBN check digit invalid; not looked up")
            isbn_13 = None
        chains = [
            asyncio.ensure_future(
                self._provider_chain(
                    candidate, isbn_13, self.ol, "open_library", _ol_parts, _ol_isbns
                )
            ),
            asyncio.ensure_future(
                self._provider_chain(
                    candidate, isbn_13, self.gb, "google_books", _gb_parts, _gb_isbns
                )
            ),
        ]
        try:
            ol_doc, gb_volume = await asyncio.gather(*chains)
        except BaseException:
            # A cancel (stray or real) of one chain: stop the other too, so no
            # lookup outlives this candidate.
            for chain in chains:
                chain.cancel()
            await asyncio.gather(*chains, return_exceptions=True)
            raise
        return ol_doc, gb_volume

    @staticmethod
    async def _provider_chain(
        candidate: BookCandidate,
        isbn_13: str | None,
        provider: OpenLibraryService | GoogleBooksService,
        source: str,
        parts: Callable[[object], _RecordParts],
        isbns: Callable[[dict], set[str]],
    ) -> dict | None:
        """One provider's record for a candidate: its ISBN lookup, then, if
        that found nothing usable, its title search (see
        :meth:`_find_records`).

        An ISBN record is final only when it lists the ISBN or fits the
        reading well (score 3 or more). A weaker one (a one-sided subtitle,
        not listing the ISBN: Google Books ``q=isbn:X`` is a keyword search
        and may return another book) is kept only if the title search finds
        nothing that scores higher.

        Any error here (a malformed provider record, say) counts as a lookup
        failure: the provider contributes nothing, the enrichment is
        incomplete, and the candidate still gets its result."""
        try:
            return await EnrichmentService._chain(
                candidate, isbn_13, provider, source, parts, isbns
            )
        except Exception as exc:  # noqa: BLE001 - never drop the candidate
            logger.warning(
                "%s error: unusable record (%s)",
                _SOURCE_NAMES[source],
                type(exc).__name__,
            )
            _note_failure(source)
            return None

    @staticmethod
    async def _chain(
        candidate: BookCandidate,
        isbn_13: str | None,
        provider: OpenLibraryService | GoogleBooksService,
        source: str,
        parts: Callable[[object], _RecordParts],
        isbns: Callable[[dict], set[str]],
    ) -> dict | None:
        score = _scorer(candidate, parts)
        has_title = bool(_split_title(candidate.title)[0])
        if not has_title and not isbn_13:
            return None
        weak: dict | None = None
        if isbn_13:
            found = await _lookup(provider.search_by_isbn(isbn_13), source)
            record = _isbn_record(candidate, found, score, parts, isbns)
            if found is _FAILED or not has_title:
                return record
            if record is not None:
                if isbn_13 in isbns(record) or score(record) >= 3:
                    return record
                weak = record
        found = await _lookup(
            provider.search(
                candidate.title,
                reading_surname(candidate),
                limit=TITLE_SEARCH_RESULTS,
                score=score,
            ),
            source,
        )
        # The service already picked the best record; check it again here,
        # so nothing that does not fit the reading is ever used.
        best = found if isinstance(found, dict) and score(found) > 0 else None
        if weak is None or best is None:
            return weak if best is None else best
        if score(weak) != score(best):
            return weak if score(weak) > score(best) else best
        # A tie: the record that lists one of the candidate's ISBNs, else the
        # title-search record over a keyword ISBN hit that lists none.
        isbn_10 = str(_isbn_digits(candidate.isbn_10 or "")).upper()
        wanted = {isbn_13, isbn_10} - {None, ""}

        def lists(record: dict) -> bool:
            return bool(wanted & {i.upper() for i in isbns(record)})

        return weak if lists(weak) and not lists(best) else best

    async def _enrich_one(self, candidate: BookCandidate) -> EnrichedBook:
        # "978-0-06-231609-7" is looked up, and confirmed, as its digits.
        cleaned = _isbn_digits(candidate.isbn_13)
        if cleaned != candidate.isbn_13 and valid_isbn_13(cleaned):
            candidate = dataclasses.replace(candidate, isbn_13=cleaned)
        ol_doc, gb_volume = await self._find_records(candidate)
        return self._build(candidate, ol_doc, gb_volume)

    def _build(
        self, candidate: BookCandidate, ol_doc: dict | None, gb_volume: dict | None
    ) -> EnrichedBook:
        """The result for a candidate from the records found for it (both
        None: nothing matched, the unsigned reading)."""
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

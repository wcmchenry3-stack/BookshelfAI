"""EnrichmentService — merges Open Library + Google Books into EnrichedBook.

The candidate (the vision model's reading of the photo, or a search hit) is
only a search key. Which records are looked at (#743): with an ISBN-13, each
provider is first looked up by it, and a record found that way is used only
when it fits the reading (:func:`reading_match_score`: agreeing titles, and
a compatible author when both name one), since the model's ISBN is often
misread or invented. For each provider that has no such record (and for a
candidate without an ISBN-13) the provider is searched by title and author
surname, and the best fitting of up to ``TITLE_SEARCH_RESULTS`` records is
used; a record that fits nothing is never used. At most four provider
requests per candidate, each cut off after ``PROVIDER_CALL_TIMEOUT_S``, and
the whole call stops at ``ENRICHMENT_DEADLINE_S``.

Whenever a provider matched, every catalog field of the
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
from collections.abc import Awaitable, Callable

from app.schemas.book import EditionPreview, EnrichedBook
from app.services.book_identifier import BookCandidate
from app.services.google_books import GoogleBooksService
from app.services.open_library import OpenLibraryService

logger = logging.getLogger(__name__)

# Shown when a provider matched but neither record names an author.
UNKNOWN_AUTHOR = "Unknown"
# EnrichedBook's limit for title / author.
_MAX_TEXT = 500


# Each candidate fires one Open Library and one Google Books request, and at
# most one more of each (#743): a title search when that provider's ISBN
# lookup found nothing or another book. A shelf photo can yield 15
# candidates, so cap how many are in flight at once.
MAX_CONCURRENT_ENRICHMENTS = 5
# How many records one title search considers (#743).
TITLE_SEARCH_RESULTS = 5
# Time limits (#743 review): one provider request may take at most
# PROVIDER_CALL_TIMEOUT_S (the HTTP client's own 10 s applies per phase, so
# a request could otherwise run longer), so a candidate's ISBN lookup plus
# title search fit in 16 s. The whole ``enrich`` call stops at
# ENRICHMENT_DEADLINE_S: candidates still running then come back as their
# unsigned reading and the enrichment counts as incomplete. With the vision
# call's 45 s (SCAN_TIMEOUT_SECONDS) a scan stays well under Cloudflare's
# 100 s origin timeout.
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


def normalise_text(value: object) -> str:
    """Public form of :func:`_normalise`: casefolded, punctuation stripped,
    whitespace collapsed."""
    return _normalise(value)


def normalise_title(value: object) -> str:
    """A title for comparison: :func:`_normalise`, minus a leading article."""
    return _LEADING_ARTICLE.sub("", _normalise(value), count=1)


# Where a subtitle or edition note starts: "Dune: Deluxe Edition",
# "Dune (40th Anniversary)", "Dune - A Novel", "Dune — Book One".
_SUBTITLE = re.compile(r"\s*[:(\[\u2013\u2014]|\s+-\s+")
# A subtitle that only describes the edition, not which book it is. Anything
# else ("Book Two", "The Two Towers") may tell volumes of a series apart, so it
# must match exactly.
_EDITION_NOTE = re.compile(
    r"^(?:(?:a|an|the)\s+)?(?:\d+(?:st|nd|rd|th)\s+)?"
    r"(?:(?:deluxe|special|collectors?|anniversary|illustrated|annotated|"
    r"unabridged|abridged|revised|expanded|updated|definitive|complete|"
    r"classic|movie\s+tie\s*in|tie\s*in|mass\s+market|large\s+print|"
    r"paperback|hardcover|reissue|new|first|second|third)\s+)*"
    r"(?:edition|ed|anniversary|novel|classic|reissue|paperback|hardcover|"
    r"printing)$"
)


def _split_title(value: object) -> tuple[str, str]:
    """(normalised main title, normalised subtitle or "")."""
    if not isinstance(value, str):
        return "", ""
    parts = _SUBTITLE.split(value, maxsplit=1)
    main = normalise_title(parts[0])
    sub = _normalise(parts[1]) if len(parts) > 1 else ""
    return main, sub


def titles_agree(a: object, b: object) -> bool:
    """Whether two provider titles name the same book: equal main titles
    where each subtitle is the same or only an edition note ("Dune" vs
    "Dune: Deluxe Edition"), so a series' volumes ("Saga: Book One" vs
    "Saga: Book Two") never agree."""
    main_a, sub_a = _split_title(a)
    main_b, sub_b = _split_title(b)
    if not main_a or main_a != main_b:
        return False
    if sub_a == sub_b:
        return True
    return all(not sub or _EDITION_NOTE.match(sub) for sub in (sub_a, sub_b))


def _surname(authors: object) -> str:
    """The normalised last word of the first author's name."""
    if not isinstance(authors, list) or not authors:
        return ""
    words = _normalise(authors[0]).split()
    return words[-1] if words else ""


def _gb_full_title(info: dict) -> object:
    """Google Books' title with its separate ``subtitle`` folded in as
    "title: subtitle", so a distinguishing subtitle is compared too."""
    title, subtitle = info.get("title"), info.get("subtitle")
    if isinstance(title, str) and isinstance(subtitle, str) and subtitle.strip():
        return f"{title}: {subtitle}"
    return title


def records_agree(ol_doc: dict, gb_volume: dict) -> bool:
    """Whether an Open Library doc and a Google Books volume describe the
    same book: they share an ISBN, or their titles agree (:func:`titles_agree`:
    the same main title, any differing subtitle only an edition note) and
    their first-author surnames match (and are non-empty)."""
    if _ol_isbns(ol_doc) & _gb_isbns(gb_volume):
        return True
    info = gb_volume.get("volumeInfo") or {}
    ol_author, gb_author = _surname(ol_doc.get("author_name")), _surname(
        info.get("authors")
    )
    return (
        bool(ol_author)
        and ol_author == gb_author
        and titles_agree(ol_doc.get("title"), _gb_full_title(info))
    )


# Where a title's head ends when a reading is matched against a record: the
# subtitle separators above plus a comma or semicolon.
_HEAD = re.compile(r"\s*[,;:(\[–—]|\s+-\s+")
# A rest of a record's title that only dates the book: "1848-1861", "1990".
_DATES = re.compile(r"^\d{4}(?: \d{4})*$")
# Name suffixes skipped when taking a surname ("Martin Luther King Jr.").
_NAME_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "phd", "md"})


def _head_and_rest(value: object) -> tuple[str, str]:
    """(normalised title before the first comma or subtitle separator,
    normalised rest or "")."""
    if not isinstance(value, str):
        return "", ""
    parts = _HEAD.split(value, maxsplit=1)
    rest = _normalise(parts[1]) if len(parts) > 1 else ""
    return normalise_title(parts[0]), rest


def _name_parts(name: object) -> tuple[list[str], str]:
    """(initials of the given names, surname) of one name, normalised and
    without suffixes: "David M. Potter" is (["d", "m"], "potter")."""
    words = [w for w in _normalise(name).split() if w not in _NAME_SUFFIXES]
    if not words or words == ["unknown"]:
        return [], ""
    return [w[0] for w in words[:-1]], words[-1]


def _names_agree(a: tuple[list[str], str], b: tuple[list[str], str]) -> bool:
    """The same surname and, when both give a first name or initial, the
    first initial of one among the other's initials ("David Potter" and
    "David Morris Potter", "M. L. King" and "Martin Luther King Jr.", but
    not "Frank Herbert" and "Brian Herbert")."""
    (a_initials, a_surname), (b_initials, b_surname) = a, b
    if not a_surname or a_surname != b_surname:
        return False
    if not a_initials or not b_initials:
        return True
    return a_initials[0] in b_initials or b_initials[0] in a_initials


def reading_surname(candidate: BookCandidate) -> str:
    """The surname of the author the model read ("David M. Potter",
    "David Potter" and "Potter" all give "potter"): the author key of a
    title search (#743)."""
    return _name_parts(candidate.author)[1]


def reading_match_score(
    candidate: BookCandidate, title: object, authors: object
) -> int:
    """How well a provider record (its title and author list) fits the
    model's reading of the photo (#743); 0 means it is not that book.

    Title, one of:

    - the titles agree (:func:`titles_agree`: the same main title, any
      differing subtitle only an edition note), so "Dune" never fits "Dune:
      Messiah" and "Saga" never fits "Saga: Book Two";
    - with the author confirmed, the record's title is the reading's title
      followed only by dates or an edition note after a comma or subtitle
      separator ("The Impending Crisis" fits "The impending crisis,
      1848-1861").

    Author: when the reading and a record author share the surname and both
    give a first name or initial, the first initials must agree as well
    (:func:`_names_agree`), so "Frank Herbert" never fits "Brian Herbert".
    When the reading names an author, the record must name that author; when
    it names none, any author passes. A record that names no author fits only
    on agreeing titles. A confirmed author adds 1 to the score. A reading
    without a title fits nothing.

    This only chooses which record is used: every catalog field of a match
    still comes from the record (#602). It cannot prove the record is the
    book in the photo: a wrong reading can still fit another real book."""
    reading_main, _ = _split_title(candidate.title)
    record_main, _ = _split_title(title)
    if not reading_main or not record_main:
        return 0
    reading_name = _name_parts(candidate.author)
    record_names = (
        [n for n in (_name_parts(a) for a in authors) if n[1]]
        if isinstance(authors, list)
        else []
    )
    agree = titles_agree(candidate.title, title)
    if not record_names:
        return 6 if agree else 0
    author_confirmed = bool(reading_name[1]) and any(
        _names_agree(reading_name, n) for n in record_names
    )
    if reading_name[1] and not author_confirmed:
        return 0
    if agree:
        return 6 + int(author_confirmed)
    head, rest = _head_and_rest(title)
    if (
        author_confirmed
        and head == reading_main
        and (_DATES.match(rest) or _EDITION_NOTE.match(rest))
    ):
        return 3
    return 0


def _ol_score(candidate: BookCandidate) -> Callable[[dict], int]:
    """:func:`reading_match_score` for an Open Library doc."""

    def score(doc: dict) -> int:
        if not isinstance(doc, dict) or not _provider_text(doc.get("title")):
            return 0
        return reading_match_score(candidate, doc.get("title"), doc.get("author_name"))

    return score


def _gb_score(candidate: BookCandidate) -> Callable[[dict], int]:
    """:func:`reading_match_score` for a Google Books volume (its subtitle
    folded into the title)."""

    def score(volume: dict) -> int:
        info = volume.get("volumeInfo") if isinstance(volume, dict) else None
        if not isinstance(info, dict) or not _provider_text(info.get("title")):
            return 0
        return reading_match_score(candidate, _gb_full_title(info), info.get("authors"))

    return score


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


def isbn_10_to_13(isbn_10: str | None) -> str | None:
    """The 978-prefixed ISBN-13 form of an ISBN-10, else None."""
    if not isinstance(isbn_10, str):
        return None
    value = isbn_10.replace("-", "").replace(" ", "").upper()
    if len(value) != 10 or not value[:9].isdigit():
        return None
    if not (value[9].isdigit() or value[9] == "X"):
        return None
    body = "978" + value[:9]
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(body))
    return body + str((10 - total % 10) % 10)


def normalise_isbn_13(isbn_13: str | None, isbn_10: str | None = None) -> str | None:
    """One comparable key for an edition: its ISBN-13, else its ISBN-10 as
    an ISBN-13 (#587). None when it has neither."""
    if isinstance(isbn_13, str):
        value = isbn_13.replace("-", "").replace(" ", "")
        if len(value) == 13 and value.isdigit():
            return value
    return isbn_10_to_13(isbn_10)


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


def _ol_title(doc: dict) -> str | None:
    return _provider_text(doc.get("title"))


def _gb_title(volume: dict) -> str | None:
    info = volume.get("volumeInfo")
    return _provider_text(info.get("title")) if isinstance(info, dict) else None


def _isbn_record(
    candidate: BookCandidate,
    found: object,
    score: Callable[[dict], int],
    title_of: Callable[[dict], str | None],
    isbns: Callable[[dict], set[str]],
) -> dict | None:
    """The record an ISBN lookup found, when it may be used (#743): it fits
    the reading, or, for a reading without a title, it has a usable title
    (``title_of``, the provider's own shape) and lists the ISBN. Otherwise
    None (the provider is then searched by title).

    A record that lists the ISBN but does not fit a reading that has a title
    (a translated or paraphrased reading, say) is deliberately not used: the
    model's ISBN is often invented, and an invented ISBN that happens to
    exist would otherwise attach another book to the reading."""
    if not isinstance(found, dict):
        return None
    if score(found) > 0:
        return found
    if not _split_title(candidate.title)[0] and title_of(found):
        return found if candidate.isbn_13 in isbns(found) else None
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

        With an ISBN-13, both providers are first looked up by it,
        concurrently. A record found that way is used only when it fits the
        model's reading (:func:`reading_match_score`), or, for a reading
        without a title, when it lists that ISBN: a misread or invented ISBN
        often names another book, or none. For each provider whose lookup
        finished without such a record, and for every candidate without an
        ISBN-13, the provider is searched by title and author surname
        (``TITLE_SEARCH_RESULTS`` records, the best fitting one kept). A
        lookup that raised is not retried. So a candidate costs at most two
        requests per provider, four in all."""
        ol_score, gb_score = _ol_score(candidate), _gb_score(candidate)
        has_title = bool(_split_title(candidate.title)[0])
        ol_doc = gb_volume = None
        search_ol = search_gb = has_title
        if candidate.isbn_13:
            ol_found, gb_found = await asyncio.gather(
                _lookup(self.ol.search_by_isbn(candidate.isbn_13), "open_library"),
                _lookup(self.gb.search_by_isbn(candidate.isbn_13), "google_books"),
            )
            ol_doc = _isbn_record(candidate, ol_found, ol_score, _ol_title, _ol_isbns)
            gb_volume = _isbn_record(
                candidate, gb_found, gb_score, _gb_title, _gb_isbns
            )
            search_ol = has_title and ol_doc is None and ol_found is not _FAILED
            search_gb = has_title and gb_volume is None and gb_found is not _FAILED

        surname = reading_surname(candidate)
        searches: list[Awaitable[object]] = []
        if search_ol:
            searches.append(
                _lookup(
                    self.ol.search(
                        candidate.title,
                        surname,
                        limit=TITLE_SEARCH_RESULTS,
                        score=ol_score,
                    ),
                    "open_library",
                )
            )
        if search_gb:
            searches.append(
                _lookup(
                    self.gb.search(
                        candidate.title,
                        surname,
                        limit=TITLE_SEARCH_RESULTS,
                        score=gb_score,
                    ),
                    "google_books",
                )
            )
        found = list(await asyncio.gather(*searches)) if searches else []
        # The service already picked the best record; check it again here,
        # so nothing that does not fit the reading is ever used.
        if search_ol:
            doc = found.pop(0)
            ol_doc = doc if isinstance(doc, dict) and ol_score(doc) > 0 else None
        if search_gb:
            volume = found.pop(0)
            gb_volume = (
                volume if isinstance(volume, dict) and gb_score(volume) > 0 else None
            )
        return ol_doc, gb_volume

    async def _enrich_one(self, candidate: BookCandidate) -> EnrichedBook:
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

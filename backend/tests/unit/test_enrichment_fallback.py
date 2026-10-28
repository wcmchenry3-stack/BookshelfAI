"""ISBN misses and misreads fall back to a title search (#743).

The vision model's ISBN is often misread or invented (both ISBNs logged for
the failing production scans have a bad check digit). A record found by that
ISBN is used only when it fits the model's reading; otherwise, and when the
lookup found nothing, the provider is searched by title and author surname,
and the best fitting of a few results is kept. Open Library and Google Books
are faked at their service methods (or at their HTTP client); the real
EnrichmentService runs. No network, no AI.
"""

import asyncio
import math
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core import catalog_token
from app.core.config import settings
from app.schemas.book import EnrichedBook
from app.services import enrichment
from app.services.book_identifier import BookCandidate
from app.services.enrichment import (
    TITLE_SEARCH_RESULTS,
    EnrichmentService,
    reading_match_score,
)
from app.services.google_books import GoogleBooksService
from app.services.open_library import OpenLibraryService

USER = "00000000-0000-0000-0000-000000000743"

# The Impending Crisis, as the providers list it.
CRISIS_ISBN = "9780061319297"
CRISIS_DOC = {
    "key": "/works/OL1966510W",
    "title": "The impending crisis, 1848-1861",
    "author_name": ["David Morris Potter"],
    "subject": ["United States -- History -- 1849-1877"],
    "isbn": [CRISIS_ISBN, "0061319295"],
}
CRISIS_VOLUME = {
    "id": "gb_crisis",
    "volumeInfo": {
        "title": "The Impending Crisis",
        "subtitle": "1848-1861",
        "authors": ["David M. Potter"],
        "description": "Pulitzer Prize.",
        "industryIdentifiers": [{"type": "ISBN_13", "identifier": CRISIS_ISBN}],
    },
}
# What a misread ISBN led to instead.
OTHER_DOC = {
    "key": "/works/OL999W",
    "title": "Benjamin Harrison",
    "author_name": ["Charles W. Calhoun"],
    "isbn": ["9780805069525"],
}
OTHER_VOLUME = {
    "id": "gb_other",
    "volumeInfo": {"title": "Alabama Birds", "authors": ["Thomas A. Imhof"]},
}
# A misread ISBN with a valid check digit (so it is looked up).
WRONG_ISBN = "9780817354367"


def _reading(
    author="David M. Potter", isbn_13=WRONG_ISBN, title="The Impending Crisis"
):
    return BookCandidate(title=title, author=author, confidence=0.8, isbn_13=isbn_13)


def _service(*, ol_isbn=None, gb_isbn=None, ol_title=None, gb_title=None):
    service = EnrichmentService()
    service.ol.search_by_isbn = AsyncMock(return_value=ol_isbn)
    service.gb.search_by_isbn = AsyncMock(return_value=gb_isbn)
    service.ol.search = AsyncMock(return_value=ol_title)
    service.gb.search = AsyncMock(return_value=gb_title)
    return service


async def _enrich_one(service, candidate):
    (book,) = await service.enrich([candidate])
    assert isinstance(book, EnrichedBook)
    return book


def _calls(service) -> int:
    return sum(
        m.await_count
        for m in (
            service.ol.search_by_isbn,
            service.gb.search_by_isbn,
            service.ol.search,
            service.gb.search,
        )
    )


class TestWrongIsbnFallsBackToTitle:
    async def test_isbn_naming_another_book_is_ignored(self):
        service = _service(
            ol_isbn=OTHER_DOC,
            gb_isbn=OTHER_VOLUME,
            ol_title=CRISIS_DOC,
            gb_title=CRISIS_VOLUME,
        )
        book = await _enrich_one(service, _reading())
        assert book.open_library_work_id == "OL1966510W"
        assert book.google_books_id == "gb_crisis"
        assert (book.title, book.author) == (
            "The impending crisis, 1848-1861",
            "David Morris Potter",
        )
        # The misread ISBN is no record's, so it is not kept.
        assert book.editions[0].isbn_13 is None
        # One ISBN lookup and one title search per provider (the cost bound).
        assert _calls(service) == 4
        service.ol.search.assert_awaited_once()
        service.gb.search.assert_awaited_once()
        assert service.last_complete is True

    async def test_invalid_check_digit_is_not_looked_up(self):
        # Both ISBNs logged for the #743 scans have a bad check digit.
        for isbn in ("9780786412345", "9780817354368"):
            service = _service(
                ol_isbn=OTHER_DOC,
                gb_isbn=OTHER_VOLUME,
                ol_title=CRISIS_DOC,
                gb_title=CRISIS_VOLUME,
            )
            book = await _enrich_one(service, _reading(isbn_13=isbn))
            service.ol.search_by_isbn.assert_not_awaited()
            service.gb.search_by_isbn.assert_not_awaited()
            assert _calls(service) == 2
            assert book.open_library_work_id == "OL1966510W"
            assert book.editions[0].isbn_13 is None

    async def test_isbn_that_misses_falls_back_to_title(self):
        service = _service(ol_title=CRISIS_DOC, gb_title=CRISIS_VOLUME)
        book = await _enrich_one(service, _reading())
        assert book.open_library_work_id == "OL1966510W"
        assert book.google_books_id == "gb_crisis"
        (signed,) = catalog_token.sign_books([book], USER)
        assert signed.catalog_token

    async def test_only_the_missing_provider_is_searched(self):
        service = _service(ol_isbn=CRISIS_DOC, gb_isbn=None, gb_title=CRISIS_VOLUME)
        book = await _enrich_one(service, _reading(isbn_13=CRISIS_ISBN))
        service.ol.search.assert_not_awaited()
        service.gb.search.assert_awaited_once()
        assert book.google_books_id == "gb_crisis"
        assert book.editions[0].isbn_13 == CRISIS_ISBN

    async def test_isbn_that_fits_needs_no_title_search(self):
        service = _service(ol_isbn=CRISIS_DOC, gb_isbn=CRISIS_VOLUME)
        book = await _enrich_one(service, _reading(isbn_13=CRISIS_ISBN))
        service.ol.search.assert_not_awaited()
        service.gb.search.assert_not_awaited()
        assert book.open_library_work_id == "OL1966510W"
        assert _calls(service) == 2

    async def test_a_failed_isbn_lookup_is_not_retried_by_title(self):
        service = _service(gb_isbn=CRISIS_VOLUME, ol_title=CRISIS_DOC)
        service.ol.search_by_isbn = AsyncMock(side_effect=ConnectionError("down"))
        book = await _enrich_one(service, _reading(isbn_13=CRISIS_ISBN))
        service.ol.search.assert_not_awaited()
        assert book.google_books_id == "gb_crisis"
        assert service.last_complete is False

    async def test_at_most_four_provider_calls_per_candidate(self):
        service = _service(ol_isbn=OTHER_DOC, gb_isbn=OTHER_VOLUME)
        book = await _enrich_one(service, _reading())
        assert _calls(service) == 4
        # Nothing fitted: the reading is shown, unsigned.
        assert (book.title, book.author) == ("The Impending Crisis", "David M. Potter")
        assert book.open_library_work_id is None and book.google_books_id is None
        (signed,) = catalog_token.sign_books([book], USER)
        assert signed.catalog_token is None


class TestImpendingCrisis:
    """The production scan that found nothing (#743)."""

    @pytest.mark.parametrize("author", ["David M. Potter", "Potter", "David Potter"])
    @pytest.mark.parametrize("isbn_13", [None, WRONG_ISBN])
    async def test_matches_the_provider_record(self, author, isbn_13):
        service = _service(ol_title=CRISIS_DOC, gb_title=CRISIS_VOLUME)
        book = await _enrich_one(service, _reading(author=author, isbn_13=isbn_13))
        assert book.open_library_work_id == "OL1966510W"
        assert book.google_books_id == "gb_crisis"
        assert book.title == "The impending crisis, 1848-1861"
        assert book.author == "David Morris Potter"
        assert book.description == "Pulitzer Prize."


class TestIsbnOnlyReading:
    """A reading with an ISBN but no usable title or author."""

    async def test_isbn_that_misses_gives_the_unsigned_reading(self):
        service = _service()
        book = await _enrich_one(
            service, _reading(title="", author="", isbn_13=WRONG_ISBN)
        )
        # No title to search by: only the two ISBN lookups ran.
        service.ol.search.assert_not_awaited()
        service.gb.search.assert_not_awaited()
        assert book.open_library_work_id is None and book.google_books_id is None
        assert book.editions[0].isbn_13 is None
        (signed,) = catalog_token.sign_books([book], USER)
        assert signed.catalog_token is None
        assert service.last_complete is True

    async def test_record_listing_the_isbn_is_used(self):
        service = _service(ol_isbn=CRISIS_DOC)
        book = await _enrich_one(
            service, _reading(title=" ", author="", isbn_13=CRISIS_ISBN)
        )
        assert book.open_library_work_id == "OL1966510W"
        assert book.title == "The impending crisis, 1848-1861"
        assert book.editions[0].isbn_13 == CRISIS_ISBN

    async def test_google_books_volume_listing_the_isbn_is_used(self):
        # Google Books keeps the title under volumeInfo (#743 review, B1).
        service = _service(ol_isbn=None, gb_isbn=CRISIS_VOLUME)
        book = await _enrich_one(
            service, _reading(title="", author="", isbn_13=CRISIS_ISBN)
        )
        assert book.google_books_id == "gb_crisis"
        assert book.open_library_work_id is None
        assert (book.title, book.author) == ("The Impending Crisis", "David M. Potter")
        assert book.editions[0].isbn_13 == CRISIS_ISBN
        (signed,) = catalog_token.sign_books([book], USER)
        assert signed.catalog_token
        assert service.last_complete is True

    async def test_record_not_listing_the_isbn_is_not_used(self):
        service = _service(ol_isbn=OTHER_DOC, gb_isbn=OTHER_VOLUME)
        book = await _enrich_one(
            service, _reading(title="", author="", isbn_13=CRISIS_ISBN)
        )
        assert book.open_library_work_id is None and book.google_books_id is None


class TestTitleSearchMustFitTheReading:
    async def test_a_record_for_another_book_is_not_used(self):
        candidate = BookCandidate(
            title="Benjamin Harrison", author="Niven Postma", confidence=0.6
        )
        service = _service(ol_title=OTHER_DOC, gb_title=OTHER_VOLUME)
        book = await _enrich_one(service, candidate)
        # Same title, another author: not this book.
        assert book.open_library_work_id is None and book.google_books_id is None
        assert (book.title, book.author) == ("Benjamin Harrison", "Niven Postma")


class TestReadingMatchScore:
    R = BookCandidate(
        title="The Impending Crisis", author="David M. Potter", confidence=1
    )

    def test_agreeing_title_scores_highest(self):
        full = reading_match_score(self.R, "Impending Crisis", ["David Potter"])
        edition = reading_match_score(
            self.R, "The Impending Crisis: Revised Edition", ["Potter"]
        )
        dated = reading_match_score(
            self.R, "The impending crisis, 1848-1861", ["David Morris Potter"]
        )
        assert full == edition > dated > 0

    @pytest.mark.parametrize(
        "rest", ["1848-1861", "1848 - 1861", "1976", "Revised Edition"]
    )
    def test_dates_or_edition_after_a_comma_fit(self, rest):
        assert reading_match_score(self.R, f"The Impending Crisis, {rest}", ["Potter"])

    @pytest.mark.parametrize(
        "reading_title, title",
        [
            ("The Impending Crisis", "The Impending Crisis: America Before the War"),
            ("The Impending Crisis: America Before the War", "The Impending Crisis"),
        ],
    )
    def test_one_sided_subtitle_is_the_lowest_fit(self, reading_title, title):
        # Round 2: a spine often leaves the subtitle out. It fits, below every
        # other tier, and only with the author confirmed.
        reading = BookCandidate(title=reading_title, author="Potter", confidence=1)
        one_sided = reading_match_score(reading, title, ["David M. Potter"])
        dated = reading_match_score(
            reading, "The impending crisis, 1848-1861", ["David M. Potter"]
        )
        assert 0 < one_sided
        assert one_sided < reading_match_score(
            reading, reading_title, ["David M. Potter"]
        )
        if ":" not in reading_title:
            assert one_sided < dated
        no_author = BookCandidate(title=reading_title, author="", confidence=1)
        assert reading_match_score(no_author, title, ["David M. Potter"]) == 0

    @pytest.mark.parametrize(
        "main, subtitle, author",
        [
            ("Dune", "Messiah", "Frank Herbert"),
            ("The Lord of the Rings", "The Two Towers", "J. R. R. Tolkien"),
        ],
    )
    def test_another_work_with_the_main_title_ranks_below_the_exact_one(
        self, main, subtitle, author
    ):
        # The one-sided-subtitle tier lets these fit when nothing better is
        # found; a search result with the exact title always wins.
        reading = BookCandidate(title=main, author=author, confidence=1)
        other = reading_match_score(reading, f"{main}: {subtitle}", [author])
        assert 0 < other < reading_match_score(reading, main, [author])

    @pytest.mark.parametrize(
        "reading_title, reading_author, title, authors",
        [
            ("Saga", "", "Saga: Book Two", ["Brian K. Vaughan"]),
            ("Saga", "Brian K. Vaughan", "Saga: Book Two", ["Brian K. Vaughan"]),
            ("Saga", "Vaughan", "Saga: Volume 3", ["Brian K. Vaughan"]),
            ("Saga", "Vaughan", "Saga: Part II", ["Brian K. Vaughan"]),
            ("Saga", "Vaughan", "Saga #2", ["Brian K. Vaughan"]),
            ("Dune", "Frank Herbert", "Dune: Part Two", ["Brian Herbert"]),
            ("Dune", "Frank Herbert", "Dune", ["Brian Herbert"]),
            ("Dune (Book 2)", "Frank Herbert", "Dune", ["Frank Herbert"]),
        ],
    )
    def test_series_volumes_and_namesakes_score_zero(
        self, reading_title, reading_author, title, authors
    ):
        reading = BookCandidate(
            title=reading_title, author=reading_author, confidence=1
        )
        assert reading_match_score(reading, title, authors) == 0

    def test_a_record_without_authors_needs_agreeing_titles(self):
        emma = BookCandidate(title="Emma", author="Jane Austen", confidence=1)
        assert reading_match_score(emma, "Emma", []) > 0
        assert reading_match_score(emma, "Emma: Revised Edition", None) > 0
        assert reading_match_score(emma, "Emma, 1815", []) == 0
        assert reading_match_score(emma, "Emma: A Novel of Highbury", []) == 0

    @pytest.mark.parametrize(
        "reading_author, record_author",
        [
            ("David Potter", "David Morris Potter"),
            ("D. M. Potter", "David Morris Potter"),
            ("Morris Potter", "David Morris Potter"),
            ("Le Guin", "Ursula K. Le Guin"),
            ("Potter", "David Morris Potter"),
        ],
    )
    def test_compatible_first_names_fit(self, reading_author, record_author):
        reading = BookCandidate(
            title="The Impending Crisis", author=reading_author, confidence=1
        )
        assert reading_match_score(reading, "The Impending Crisis", [record_author])

    def test_confirmed_author_scores_higher(self):
        named = reading_match_score(self.R, "The Impending Crisis", ["David M. Potter"])
        unnamed = reading_match_score(self.R, "The Impending Crisis", [])
        assert named > unnamed > 0

    @pytest.mark.parametrize(
        "title, authors",
        [
            ("The Impending Crisis", ["Hinton Rowan Helper"]),  # another author
            ("Impending", ["David M. Potter"]),
            ("The Impending Crisis of the South", ["David M. Potter"]),
            (None, ["David M. Potter"]),
            ("", ["David M. Potter"]),
        ],
    )
    def test_another_book_scores_zero(self, title, authors):
        assert reading_match_score(self.R, title, authors) == 0

    def test_a_longer_title_without_a_separator_is_another_book(self):
        dune = BookCandidate(title="Dune", author="Frank Herbert", confidence=1)
        assert reading_match_score(dune, "Dune Messiah", ["Frank Herbert"]) == 0

    def test_head_match_needs_the_author(self):
        no_author = BookCandidate(title="The Impending Crisis", author="", confidence=1)
        assert (
            reading_match_score(
                no_author, "The impending crisis, 1848-1861", ["Potter"]
            )
            == 0
        )

    @pytest.mark.parametrize("author", ["Martin Luther King Jr.", "King", "M. L. King"])
    def test_surname_skips_suffixes(self, author):
        reading = BookCandidate(title="Strength to Love", author=author, confidence=1)
        assert (
            reading_match_score(
                reading, "Strength to Love", ["Martin Luther King, Jr."]
            )
            > 0
        )

    def test_reading_without_a_title_fits_nothing(self):
        blank = BookCandidate(title="  ", author="Potter", confidence=1)
        assert reading_match_score(blank, "The Impending Crisis", ["Potter"]) == 0


def _http_client(json_data: dict):
    resp = MagicMock()
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


class TestServicesPickTheBestResult:
    """The title search asks for a few results and keeps the best fitting."""

    CANDIDATE = _reading(author="Potter", isbn_13=None)

    async def test_open_library_skips_records_that_do_not_fit(self):
        docs = [
            {**OTHER_DOC},
            {
                "key": "/works/OL2W",
                "title": "The Impending Crisis",
                "author_name": ["Hinton Helper"],
            },
            CRISIS_DOC,
        ]
        service = EnrichmentService()
        client = _http_client({"docs": docs})
        with (
            patch("app.services.open_library.httpx.AsyncClient", return_value=client),
            patch.object(service.gb, "search", AsyncMock(return_value=None)),
        ):
            book = await _enrich_one(service, self.CANDIDATE)
        assert book.open_library_work_id == "OL1966510W"
        params = client.get.await_args.kwargs["params"]
        assert params["limit"] == TITLE_SEARCH_RESULTS
        assert (params["title"], params["author"]) == ("The Impending Crisis", "potter")

    async def test_google_books_prefers_the_closest_title(self):
        exact = {
            "id": "gb_exact",
            "volumeInfo": {
                "title": "The Impending Crisis",
                "authors": ["David M. Potter"],
            },
        }
        items = [OTHER_VOLUME, CRISIS_VOLUME, exact]
        service = EnrichmentService()
        client = _http_client({"items": items})
        with (
            patch("app.services.google_books.httpx.AsyncClient", return_value=client),
            patch.object(service.ol, "search", AsyncMock(return_value=None)),
        ):
            book = await _enrich_one(service, self.CANDIDATE)
        assert book.google_books_id == "gb_exact"
        params = client.get.await_args.kwargs["params"]
        assert params["q"] == 'intitle:"The Impending Crisis" inauthor:"potter"'
        assert params["maxResults"] == TITLE_SEARCH_RESULTS

    async def test_nothing_fitting_is_no_match(self):
        service = EnrichmentService()
        # Both services use the same httpx module: one fake answers both.
        client = _http_client({"docs": [OTHER_DOC], "items": [OTHER_VOLUME]})
        with patch("app.services.open_library.httpx.AsyncClient", return_value=client):
            book = await _enrich_one(service, self.CANDIDATE)
        assert client.get.await_count == 2
        assert book.open_library_work_id is None and book.google_books_id is None

    async def test_query_without_an_author(self):
        ol_client = _http_client({"docs": []})
        gb_client = _http_client({})
        with patch(
            "app.services.open_library.httpx.AsyncClient", return_value=ol_client
        ):
            assert await OpenLibraryService().search('Say "Hi"', "", limit=50) is None
        with patch(
            "app.services.google_books.httpx.AsyncClient", return_value=gb_client
        ):
            assert await GoogleBooksService().search('Say "Hi"', " ", limit=50) is None
        ol_params = ol_client.get.await_args.kwargs["params"]
        assert "author" not in ol_params and ol_params["limit"] == 5
        gb_params = gb_client.get.await_args.kwargs["params"]
        assert gb_params["q"] == 'intitle:"Say Hi"' and gb_params["maxResults"] == 5


class TestDeadlines:
    """Slow providers cannot hold a scan past the enrichment deadline (#743
    review): an unfinished candidate comes back as its unsigned reading."""

    @staticmethod
    def _slow(result, seconds=5.0):
        async def call(*_args, **_kwargs):
            await asyncio.sleep(seconds)
            return result

        return AsyncMock(side_effect=call)

    async def test_overall_deadline_returns_the_unsigned_reading(self, monkeypatch):
        monkeypatch.setattr(enrichment, "ENRICHMENT_DEADLINE_S", 0.2)
        service = _service(ol_title=CRISIS_DOC, gb_title=CRISIS_VOLUME)
        dune = BookCandidate(title="Dune", author="Frank Herbert", confidence=0.7)

        async def ol_search(title, author, **_kwargs):
            if title == "Dune":
                await asyncio.sleep(5)
            return CRISIS_DOC

        service.ol.search = AsyncMock(side_effect=ol_search)
        loop = asyncio.get_running_loop()
        started = loop.time()
        books = await service.enrich([_reading(isbn_13=None), dune], limit=15)
        assert loop.time() - started < 2
        assert [b.candidate_index for b in books] == [0, 1]
        assert books[0].open_library_work_id == "OL1966510W"
        # The slow candidate: its reading, no provider data, never signed.
        assert (books[1].title, books[1].author) == ("Dune", "Frank Herbert")
        assert books[1].open_library_work_id is None
        assert books[1].google_books_id is None
        _, signed = catalog_token.sign_books(books, USER)
        assert signed.catalog_token is None
        # Not saved as a finished enrichment step.
        assert service.last_complete is False

    async def test_a_slow_call_times_out_alone(self, monkeypatch):
        monkeypatch.setattr(enrichment, "PROVIDER_CALL_TIMEOUT_S", 0.1)
        service = _service(gb_isbn=CRISIS_VOLUME, ol_title=CRISIS_DOC)
        service.ol.search_by_isbn = self._slow(CRISIS_DOC)
        book = await _enrich_one(service, _reading(isbn_13=CRISIS_ISBN))
        # Open Library timed out (a lookup failure, not retried by title);
        # Google Books still matched.
        service.ol.search.assert_not_awaited()
        assert book.open_library_work_id is None
        assert book.google_books_id == "gb_crisis"
        assert service.last_complete is False

    def test_worst_case_timing(self):
        # A candidate takes at most two sequential requests (each provider's
        # ISBN lookup then title search; the two providers run concurrently),
        # and candidates run in waves of MAX_CONCURRENT_ENRICHMENTS.
        waves = math.ceil(
            settings.scan_max_books / enrichment.MAX_CONCURRENT_ENRICHMENTS
        )
        per_candidate_max = 2 * enrichment.PROVIDER_CALL_TIMEOUT_S
        assert per_candidate_max < enrichment.ENRICHMENT_DEADLINE_S
        # A full scan whose every candidate needs its title search fits the
        # deadline only for per-request latencies up to this; beyond it the
        # deadline cuts the last wave (returned unsigned, never a timeout).
        fits_latency = enrichment.ENRICHMENT_DEADLINE_S / (waves * 2)
        assert fits_latency >= 3
        # Either way the scan stays well under Cloudflare's 100 s.
        assert settings.scan_timeout_seconds + enrichment.ENRICHMENT_DEADLINE_S <= 70

    async def test_provider_chains_run_concurrently(self):
        # Open Library's ISBN lookup does not finish until Google Books' title
        # search has started, and vice versa. That only completes when each
        # provider's chain runs on its own; with both ISBN lookups gathered
        # before either title search, the fakes would time out.
        ol_isbn_started, gb_title_started = asyncio.Event(), asyncio.Event()

        async def ol_isbn(*_args, **_kwargs):
            ol_isbn_started.set()
            await asyncio.wait_for(gb_title_started.wait(), 2)
            return CRISIS_DOC

        async def gb_title(*_args, **_kwargs):
            gb_title_started.set()
            await asyncio.wait_for(ol_isbn_started.wait(), 2)
            return CRISIS_VOLUME

        service = _service(gb_isbn=None, ol_title=None)
        service.ol.search_by_isbn = AsyncMock(side_effect=ol_isbn)
        service.gb.search = AsyncMock(side_effect=gb_title)
        book = await _enrich_one(service, _reading(isbn_13=CRISIS_ISBN))
        assert service.last_complete is True
        assert book.open_library_work_id == "OL1966510W"
        assert book.google_books_id == "gb_crisis"

    async def test_a_cancelled_scan_leaves_no_lookup_running(self):
        cancelled: list[str] = []

        def slow(name):
            async def call(*_args, **_kwargs):
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    cancelled.append(name)
                    raise

            return AsyncMock(side_effect=call)

        service = _service()
        service.ol.search, service.gb.search = slow("ol"), slow("gb")
        task = asyncio.ensure_future(service.enrich([_reading(isbn_13=None)]))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(10):
            await asyncio.sleep(0)
        assert sorted(cancelled) == ["gb", "ol"]


SAPIENS_ISBN = "9780062316097"
SAPIENS_DOC = {
    "key": "/works/OL17075811W",
    "title": "Sapiens",
    "author_name": ["Yuval Noah Harari"],
    "isbn": [SAPIENS_ISBN],
}
SAPIENS_VOLUME = {
    "id": "gb_sapiens",
    "volumeInfo": {
        "title": "Sapiens",
        "subtitle": "A Brief History of Humankind",
        "authors": ["Yuval Noah Harari"],
        "description": "Humankind.",
        "industryIdentifiers": [{"type": "ISBN_13", "identifier": SAPIENS_ISBN}],
    },
}


class TestOneSidedSubtitles:
    """A subtitle on one side only (round 2, blocking 1): the spine, the
    search candidate or one provider leaves it out."""

    @pytest.mark.parametrize(
        "reading_title", ["Sapiens", "Sapiens: A Brief History of Humankind"]
    )
    @pytest.mark.parametrize("author", ["Yuval Noah Harari", ""])
    async def test_isbn_records_listing_the_isbn_are_used(self, reading_title, author):
        service = _service(ol_isbn=SAPIENS_DOC, gb_isbn=SAPIENS_VOLUME)
        candidate = BookCandidate(
            title=reading_title, author=author, confidence=0.9, isbn_13=SAPIENS_ISBN
        )
        book = await _enrich_one(service, candidate)
        assert book.open_library_work_id == "OL17075811W"
        assert book.google_books_id == "gb_sapiens"
        assert book.description == "Humankind."
        assert book.editions[0].isbn_13 == SAPIENS_ISBN
        assert _calls(service) == 2

    async def test_isbn_record_by_another_author_is_not_used(self):
        doc = {**SAPIENS_DOC, "author_name": ["Someone Else"]}
        service = _service(ol_isbn=doc)
        candidate = BookCandidate(
            title="Sapiens: A Brief History of Humankind",
            author="Yuval Noah Harari",
            confidence=0.9,
            isbn_13=SAPIENS_ISBN,
        )
        book = await _enrich_one(service, candidate)
        assert book.open_library_work_id is None

    @pytest.mark.parametrize(
        "reading_title", ["Sapiens", "Sapiens: A Brief History of Humankind"]
    )
    async def test_title_search_fits_with_the_author(self, reading_title):
        service = _service(ol_title=SAPIENS_DOC, gb_title=SAPIENS_VOLUME)
        candidate = BookCandidate(title=reading_title, author="Harari", confidence=0.9)
        book = await _enrich_one(service, candidate)
        assert book.open_library_work_id == "OL17075811W"
        assert book.google_books_id == "gb_sapiens"

    async def test_open_library_subtitle_is_folded_in(self):
        doc = {**SAPIENS_DOC, "subtitle": "A Brief History of Humankind"}
        candidate = BookCandidate(
            title="Sapiens: A Brief History of Humankind", author="", confidence=1
        )
        # With the subtitle folded in the titles agree, so even a reading
        # without an author fits.
        assert enrichment._scorer(candidate, enrichment._ol_parts)(doc) > 0
        assert enrichment._scorer(candidate, enrichment._ol_parts)(SAPIENS_DOC) == 0
        assert enrichment.records_agree(
            {k: v for k, v in doc.items() if k != "isbn"},
            {"volumeInfo": {**SAPIENS_VOLUME["volumeInfo"], "industryIdentifiers": []}},
        )

    async def test_open_library_is_asked_for_the_subtitle(self):
        client = _http_client({"docs": []})
        with patch("app.services.open_library.httpx.AsyncClient", return_value=client):
            await OpenLibraryService().search("Sapiens", "harari")
            await OpenLibraryService().search_by_isbn(SAPIENS_ISBN)
        for call in client.get.await_args_list:
            assert "subtitle" in call.kwargs["params"]["fields"].split(",")

    async def test_saga_volume_is_not_the_series(self):
        volume = {
            "id": "gb_saga2",
            "volumeInfo": {
                "title": "Saga",
                "subtitle": "Book Two",
                "authors": ["Brian K. Vaughan"],
            },
        }
        service = _service(gb_title=volume)
        book = await _enrich_one(
            service, BookCandidate(title="Saga", author="Vaughan", confidence=0.9)
        )
        assert book.google_books_id is None

    def test_books_search_signs_a_volume_without_its_subtitle(self):
        """/books/search builds its candidate from volumeInfo.title alone."""
        from fastapi.testclient import TestClient

        from app.api import books as books_api
        from app.auth.dependencies import get_current_user
        from app.main import app
        from app.models.user import User

        app.dependency_overrides[get_current_user] = lambda: User(
            id=uuid.UUID(USER), email="s@x.io"
        )
        enrich = books_api._enrichment
        try:
            with (
                patch.object(
                    books_api._gb,
                    "search_query",
                    AsyncMock(return_value=[SAPIENS_VOLUME]),
                ),
                patch.object(
                    enrich.ol, "search_by_isbn", AsyncMock(return_value=SAPIENS_DOC)
                ),
                patch.object(
                    enrich.gb,
                    "search_by_isbn",
                    AsyncMock(return_value=SAPIENS_VOLUME),
                ),
                patch.object(enrich.ol, "search", AsyncMock(return_value=None)),
                patch.object(enrich.gb, "search", AsyncMock(return_value=None)),
            ):
                resp = TestClient(app).get("/books/search?q=sapiens")
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code == 200, resp.text
        (book,) = resp.json()
        assert book["google_books_id"] == "gb_sapiens"
        assert book["open_library_work_id"] == "OL17075811W"
        assert book["description"] == "Humankind."
        assert book["catalog_token"]


class TestNameAndAccentShapes:
    """Round 2, items 2 and 3."""

    @pytest.mark.parametrize(
        "reading_author, record_author",
        [
            ("Gabriel Garcia Marquez", "Gabriel García Márquez"),
            ("Gabriel García Márquez", "Gabriel Garcia Marquez"),
            ("Herbert, Frank", "Frank Herbert"),
            ("Frank Herbert", "Herbert, Frank"),
            ("Le Guin", "Ursula K. LeGuin"),
            ("Ursula LeGuin", "Ursula K. Le Guin"),
            ("Martin Luther King, Jr.", "Martin Luther King Jr."),
        ],
    )
    def test_same_author_fits(self, reading_author, record_author):
        reading = BookCandidate(title="Some Title", author=reading_author, confidence=1)
        assert reading_match_score(reading, "Some Title", [record_author]) == 7

    def test_accented_title_fits(self):
        reading = BookCandidate(
            title="Cien anos de soledad", author="Garcia Marquez", confidence=1
        )
        assert reading_match_score(
            reading, "Cien años de soledad", ["Gabriel García Márquez"]
        )

    @pytest.mark.parametrize("reading_author", ["Herbert, Brian", "Brian Herbert"])
    def test_other_authors_still_do_not(self, reading_author):
        reading = BookCandidate(title="Dune", author=reading_author, confidence=1)
        assert reading_match_score(reading, "Dune", ["Herbert, Frank"]) == 0

    async def test_providers_agree_across_accents(self):
        # Round 3: records_agree ignores diacritics too, so the Google Books
        # volume is kept (no shared ISBN here).
        doc = {
            "key": "/works/OL274505W",
            "title": "Cien anos de soledad",
            "author_name": ["Gabriel Garcia Marquez"],
        }
        volume = {
            "id": "gb_cien",
            "volumeInfo": {
                "title": "Cien años de soledad",
                "authors": ["Gabriel García Márquez"],
                "description": "Macondo.",
            },
        }
        assert enrichment.records_agree(doc, volume)
        service = _service(ol_title=doc, gb_title=volume)
        book = await _enrich_one(
            service,
            BookCandidate(
                title="Cien años de soledad", author="García Márquez", confidence=0.9
            ),
        )
        assert book.open_library_work_id == "OL274505W"
        assert (book.google_books_id, book.description) == ("gb_cien", "Macondo.")


class TestIsbnSeparators:
    """Round 3: a hyphenated or spaced ISBN is looked up as its digits."""

    @pytest.mark.parametrize(
        "isbn", ["978-0-06-231609-7", "978 0 06 231609 7", " 978-0062316097 "]
    )
    async def test_separated_isbn_is_looked_up_as_digits(self, isbn):
        service = _service(ol_isbn=SAPIENS_DOC, gb_isbn=SAPIENS_VOLUME)
        candidate = BookCandidate(
            title="Sapiens", author="Yuval Noah Harari", confidence=0.9, isbn_13=isbn
        )
        book = await _enrich_one(service, candidate)
        service.ol.search_by_isbn.assert_awaited_once_with(SAPIENS_ISBN)
        service.gb.search_by_isbn.assert_awaited_once_with(SAPIENS_ISBN)
        assert book.open_library_work_id == "OL17075811W"
        assert book.google_books_id == "gb_sapiens"
        # Confirmed by the records, in its digits-only form.
        assert book.editions[0].isbn_13 == SAPIENS_ISBN

    async def test_separated_invalid_isbn_is_not_looked_up(self):
        service = _service(ol_title=SAPIENS_DOC, gb_title=SAPIENS_VOLUME)
        candidate = BookCandidate(
            title="Sapiens",
            author="Harari",
            confidence=0.9,
            isbn_13="978-0-7864-1234-5",
        )
        book = await _enrich_one(service, candidate)
        service.ol.search_by_isbn.assert_not_awaited()
        service.gb.search_by_isbn.assert_not_awaited()
        assert book.open_library_work_id == "OL17075811W"
        assert book.editions[0].isbn_13 is None


SAGA_ISBN = "9781607066019"


class TestRound4SeriesAndWeakIsbnRecords:
    """Round 4, items 1 and 2."""

    @pytest.mark.parametrize(
        "reading_title, record_title, record_subtitle, author",
        [
            ("Saga: Book One", "Saga", "Book Two", "Brian K. Vaughan"),
            ("Saga", "Saga", "Book Two", "Brian K. Vaughan"),
            ("Dune: Deluxe Edition", "Dune", "Messiah", "Frank Herbert"),
        ],
    )
    async def test_isbn_record_of_another_volume_is_not_used(
        self, reading_title, record_title, record_subtitle, author
    ):
        doc = {
            "key": "/works/OL2W",
            "title": record_title,
            "subtitle": record_subtitle,
            "author_name": [author],
            "isbn": [SAGA_ISBN],
        }
        service = _service(ol_isbn=doc)
        candidate = BookCandidate(
            title=reading_title, author=author, confidence=0.9, isbn_13=SAGA_ISBN
        )
        book = await _enrich_one(service, candidate)
        assert book.open_library_work_id is None
        assert book.editions[0].isbn_13 is None

    async def test_exact_title_search_beats_a_weak_isbn_record(self):
        graphic = {
            "id": "gb_graphic",
            "volumeInfo": {
                "title": "Sapiens",
                "subtitle": "A Graphic History",
                "authors": ["Yuval Noah Harari"],
            },
        }
        exact = {
            "id": "gb_exact",
            "volumeInfo": {"title": "Sapiens", "authors": ["Yuval Noah Harari"]},
        }
        service = _service(gb_isbn=graphic, gb_title=exact)
        candidate = BookCandidate(
            title="Sapiens", author="Harari", confidence=0.9, isbn_13=SAPIENS_ISBN
        )
        book = await _enrich_one(service, candidate)
        service.gb.search.assert_awaited_once()
        assert book.google_books_id == "gb_exact"
        assert _calls(service) <= 4

    async def test_weak_isbn_record_is_kept_when_the_search_finds_nothing(self):
        graphic = {
            "id": "gb_graphic",
            "volumeInfo": {
                "title": "Sapiens",
                "subtitle": "A Graphic History",
                "authors": ["Yuval Noah Harari"],
            },
        }
        service = _service(gb_isbn=graphic, gb_title=None)
        candidate = BookCandidate(
            title="Sapiens", author="Harari", confidence=0.9, isbn_13=SAPIENS_ISBN
        )
        book = await _enrich_one(service, candidate)
        service.gb.search.assert_awaited_once()
        assert book.google_books_id == "gb_graphic"
        # It does not list the ISBN, so the ISBN is not kept.
        assert book.editions[0].isbn_13 is None


class TestRound4VolumeMarkers:
    """Round 4, item 3: years and ordinals are not volume markers."""

    @pytest.mark.parametrize(
        "reading_title, author, record_title",
        [
            ("Postwar", "Tony Judt", "Postwar: A History of Europe Since 1945"),
            (
                "A Distant Mirror",
                "Barbara W. Tuchman",
                "A Distant Mirror: The Calamitous 14th Century",
            ),
            ("Berlin", "Antony Beevor", "Berlin: The Downfall 1945"),
        ],
    )
    def test_history_subtitles_fit(self, reading_title, author, record_title):
        reading = BookCandidate(title=reading_title, author=author, confidence=1)
        assert reading_match_score(reading, record_title, [author]) == 2

    @pytest.mark.parametrize(
        "subtitle",
        [
            "Book Two",
            "Books One to Three",
            "Volume 3",
            "Vol. 3",
            "Vols 1-2",
            "Part II",
            "Parts One and Two",
            "Tome Two",
            "Episode Four",
            "Pt 1",
            "Bk 3",
            "No. 5",
            "Number Nine",
            "#2",
            "2",
            "II",
            "XIV",
            "The Final Chapter, Book 4",
        ],
    )
    def test_volume_markers_do_not_fit(self, subtitle):
        reading = BookCandidate(title="Saga", author="Vaughan", confidence=1)
        assert (
            reading_match_score(reading, f"Saga: {subtitle}", ["Brian K. Vaughan"]) == 0
        )

    def test_author_less_reading_fits_a_record_with_a_subtitle(self):
        reading = BookCandidate(title="Sapiens", author="", confidence=1)
        score = enrichment._scorer(reading, enrichment._ol_parts)
        with_sub = {**SAPIENS_DOC, "subtitle": "A Brief History of Humankind"}
        assert 0 < score(with_sub) < score(SAPIENS_DOC)
        saga = {"title": "Saga", "subtitle": "Book Two", "author_name": ["Vaughan"]}
        saga_reading = BookCandidate(title="Saga", author="", confidence=1)
        assert enrichment._scorer(saga_reading, enrichment._ol_parts)(saga) == 0


class TestRound4NameShapes:
    @pytest.mark.parametrize(
        "reading_author, record_author",
        [
            ("John Smith", "Smith, John, Jr."),
            ("Dan Brown", "Brown, Dan, 1964-"),
            ("Dan Brown", "Brown, Dan, 1964-2020"),
            ("Lonely Planet Publications", "Lonely Planet Publications, Inc."),
            ("Jo Nesbo", "Jo Nesbø"),
            ("Stanisław Lem", "Stanislaw Lem"),
        ],
    )
    def test_same_author_fits(self, reading_author, record_author):
        reading = BookCandidate(title="A Title", author=reading_author, confidence=1)
        assert reading_match_score(reading, "A Title", [record_author]) == 7


class TestRound4Robustness:
    async def test_malformed_identifiers_do_not_drop_the_candidate(self):
        volume = {
            "id": "gb_bad",
            "volumeInfo": {
                "title": "Sapiens",
                "authors": ["Yuval Noah Harari"],
                "industryIdentifiers": 5,
            },
        }
        service = _service(ol_isbn=SAPIENS_DOC, gb_isbn=volume)
        candidate = BookCandidate(
            title="Sapiens", author="Harari", confidence=0.9, isbn_13=SAPIENS_ISBN
        )
        (book,) = await service.enrich([candidate])
        assert book.open_library_work_id == "OL17075811W"
        assert book.editions[0].isbn_13 == SAPIENS_ISBN

    async def test_an_error_in_a_chain_is_a_lookup_failure(self, monkeypatch):
        real = enrichment._isbn_record

        def flaky(candidate, found, score, parts, isbns):
            if parts is enrichment._gb_parts:
                raise TypeError("malformed record")
            return real(candidate, found, score, parts, isbns)

        monkeypatch.setattr(enrichment, "_isbn_record", flaky)
        service = _service(ol_isbn=SAPIENS_DOC, gb_isbn=SAPIENS_VOLUME)
        candidate = BookCandidate(
            title="Sapiens", author="Harari", confidence=0.9, isbn_13=SAPIENS_ISBN
        )
        books = await service.enrich([candidate])
        assert len(books) == 1
        assert books[0].open_library_work_id == "OL17075811W"
        assert books[0].google_books_id is None
        assert service.last_complete is False

    @pytest.mark.parametrize(
        "value", ["٩٧٨٠٤٤١٠١٣٥٩٣", "978044101359²", "978044101359", None, 9780441013593]
    )
    def test_valid_isbn_13_rejects_non_ascii_and_junk(self, value):
        assert enrichment.valid_isbn_13(value) is False

    def test_invalid_isbn_13_is_never_stored(self):
        bad = "9780000000001"
        assert enrichment._edition_isbns(bad, None, {bad}) == (None, None)
        good = "9780441013593"
        assert enrichment._edition_isbns(good, None, {good}) == (good, None)


class TestRound4RecordsAgree:
    def test_one_sided_subtitle_agrees(self):
        doc = {
            "title": "Sapiens",
            "subtitle": "A Brief History of Humankind",
            "author_name": ["Yuval Noah Harari"],
        }
        volume = {"volumeInfo": {"title": "Sapiens", "authors": ["Yuval Noah Harari"]}}
        assert enrichment.records_agree(doc, volume)

    def test_last_first_author_agrees(self):
        doc = {"title": "Dune", "author_name": ["Herbert, Frank"]}
        volume = {"volumeInfo": {"title": "Dune", "authors": ["Frank Herbert"]}}
        assert enrichment.records_agree(doc, volume)

    @pytest.mark.parametrize(
        "ol_title, gb_title, gb_author",
        [
            ("Saga: Book One", "Saga", "Brian K. Vaughan"),
            ("Saga: Book One", "Saga: Book Two", "Brian K. Vaughan"),
            ("Dune", "Dune", "Brian Herbert"),
        ],
    )
    def test_other_volumes_and_namesakes_disagree(self, ol_title, gb_title, gb_author):
        author = "Brian K. Vaughan" if "Saga" in ol_title else "Frank Herbert"
        doc = {"title": ol_title, "author_name": [author]}
        volume = {"volumeInfo": {"title": gb_title, "authors": [gb_author]}}
        assert not enrichment.records_agree(doc, volume)

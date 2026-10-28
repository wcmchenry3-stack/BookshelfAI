"""ISBN misses and misreads fall back to a title search (#743).

The vision model's ISBN is often misread or invented (both ISBNs logged for
the failing production scans have a bad check digit). A record found by that
ISBN is used only when it fits the model's reading; otherwise, and when the
lookup found nothing, the provider is searched by title and author surname,
and the best fitting of a few results is kept. Open Library and Google Books
are faked at their service methods (or at their HTTP client); the real
EnrichmentService runs. No network, no AI.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core import catalog_token
from app.schemas.book import EnrichedBook
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
        "subtitle": "America Before the Civil War, 1848-1861",
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
WRONG_ISBN = "9780817354361"


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
        # The title search used the reading's title and surname.
        service.ol.search.assert_awaited_once()
        args, kwargs = service.ol.search.await_args
        assert args == ("The Impending Crisis", "potter")
        assert kwargs["limit"] == TITLE_SEARCH_RESULTS
        service.gb.search.assert_awaited_once()
        assert service.gb.search.await_args.args == ("The Impending Crisis", "potter")

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
        assert service.ol.search.await_args.args[1] == "potter"


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
        sub = reading_match_score(
            self.R, "The Impending Crisis: America Before the Civil War", ["Potter"]
        )
        head = reading_match_score(
            self.R, "The impending crisis, 1848-1861", ["David Morris Potter"]
        )
        assert full > sub > head > 0

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

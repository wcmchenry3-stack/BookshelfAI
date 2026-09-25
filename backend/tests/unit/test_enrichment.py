"""Unit tests for EnrichmentService — OL and GB services are mocked."""

from unittest.mock import AsyncMock

import httpx
import pytest

from app.schemas.book import EnrichedBook
from app.services.book_identifier import BookCandidate
from app.services.enrichment import EnrichmentService

CANDIDATE = BookCandidate(title="Dune", author="Frank Herbert", confidence=0.97)
CANDIDATE_WITH_ISBN = BookCandidate(
    title="Dune", author="Frank Herbert", confidence=0.97, isbn_13="9780441013593"
)

OL_DOC = {
    "key": "/works/OL45804W",
    "title": "Dune",
    "author_name": ["Frank Herbert"],
    "subject": ["Science fiction", "Desert planets"],
}

GB_VOLUME = {
    "id": "gb_dune_001",
    "volumeInfo": {
        "description": "A desert planet epic.",
        "publisher": "Ace Books",
        "publishedDate": "1990",
        "pageCount": 604,
        "imageLinks": {"thumbnail": "http://books.google.com/thumb.jpg"},
    },
}


@pytest.fixture
def service():
    return EnrichmentService()


class TestEnrich:
    async def test_returns_enriched_book_for_valid_candidate(self, service):
        service.ol.search = AsyncMock(return_value=OL_DOC)
        service.gb.search = AsyncMock(return_value=GB_VOLUME)

        results = await service.enrich([CANDIDATE])

        assert len(results) == 1
        book = results[0]
        assert isinstance(book, EnrichedBook)
        assert book.title == "Dune"
        assert book.author == "Frank Herbert"
        assert book.open_library_work_id == "OL45804W"
        assert book.confidence == 0.97
        assert book.already_in_library is False

    async def test_uses_isbn_path_when_isbn_13_present(self, service):
        service.ol.search_by_isbn = AsyncMock(return_value=OL_DOC)
        service.gb.search_by_isbn = AsyncMock(return_value=GB_VOLUME)
        service.ol.search = AsyncMock()
        service.gb.search = AsyncMock()

        await service.enrich([CANDIDATE_WITH_ISBN])

        service.ol.search_by_isbn.assert_called_once_with("9780441013593")
        service.gb.search_by_isbn.assert_called_once_with("9780441013593")
        service.ol.search.assert_not_called()
        service.gb.search.assert_not_called()

    async def test_caps_at_three_candidates(self, service):
        service.ol.search = AsyncMock(return_value=OL_DOC)
        service.gb.search = AsyncMock(return_value=GB_VOLUME)

        five_candidates = [
            BookCandidate(title=f"Book {i}", author="Author", confidence=0.9)
            for i in range(5)
        ]
        results = await service.enrich(five_candidates)

        assert len(results) == 3

    async def test_respects_custom_limit(self, service):
        service.ol.search = AsyncMock(return_value=OL_DOC)
        service.gb.search = AsyncMock(return_value=GB_VOLUME)

        candidates = [
            BookCandidate(title=f"Book {i}", author="Author", confidence=0.9)
            for i in range(12)
        ]
        results = await service.enrich(candidates, limit=15)

        assert [r.title for r in results] == [f"Book {i}" for i in range(12)]

    async def test_skips_failed_candidates(self, service):
        # When both APIs fail, the service logs warnings and continues with None values,
        # producing an EnrichedBook with the candidate's title/author but no enriched data.
        # A genuine _enrich_one exception (e.g. unexpected crash) would be caught by
        # gather(return_exceptions=True) and logged — result excluded from list.
        # Simulate that by making _enrich_one itself raise.
        from unittest.mock import patch as _patch

        async def _failing(*_):
            raise RuntimeError("boom")

        with _patch.object(service, "_enrich_one", side_effect=_failing):
            results = await service.enrich([CANDIDATE])

        assert results == []

    async def test_uses_ol_author_when_available(self, service):
        service.ol.search = AsyncMock(return_value=OL_DOC)
        service.gb.search = AsyncMock(return_value=None)

        candidate = BookCandidate(title="Dune", author="F. Herbert", confidence=0.8)
        results = await service.enrich([candidate])

        # OL has "Frank Herbert" — should prefer that
        assert results[0].author == "Frank Herbert"

    async def test_falls_back_to_candidate_author_when_ol_missing(self, service):
        service.ol.search = AsyncMock(return_value=None)
        service.gb.search = AsyncMock(return_value=None)

        candidate = BookCandidate(title="Dune", author="Frank Herbert", confidence=0.8)
        results = await service.enrich([candidate])

        assert results[0].author == "Frank Herbert"

    async def test_includes_subjects_from_ol(self, service):
        service.ol.search = AsyncMock(return_value=OL_DOC)
        service.gb.search = AsyncMock(return_value=None)

        results = await service.enrich([CANDIDATE])

        assert "Science fiction" in results[0].subjects

    async def test_enriches_multiple_candidates(self, service):
        service.ol.search = AsyncMock(return_value=OL_DOC)
        service.gb.search = AsyncMock(return_value=GB_VOLUME)

        c1 = BookCandidate(title="Dune", author="Frank Herbert", confidence=0.97)
        c2 = BookCandidate(title="Foundation", author="Isaac Asimov", confidence=0.85)

        results = await service.enrich([c1, c2])

        assert len(results) == 2


class TestEnrichErrorPaths:
    """Verify graceful degradation when one or both APIs fail."""

    async def test_handles_ol_exception_gb_success(self, service):
        """OL raises an error; GB succeeds → result has GB data but no OL work ID."""
        service.ol.search = AsyncMock(
            side_effect=httpx.HTTPStatusError("429", request=None, response=None)
        )
        service.gb.search = AsyncMock(return_value=GB_VOLUME)

        results = await service.enrich([CANDIDATE])

        assert len(results) == 1
        book = results[0]
        assert book.title == "Dune"
        assert book.open_library_work_id is None  # OL failed
        assert book.google_books_id == "gb_dune_001"  # GB succeeded
        assert book.description == "A desert planet epic."

    async def test_handles_gb_exception_ol_success(self, service):
        """GB raises an error; OL succeeds → result has OL data but no GB enrichment."""
        service.ol.search = AsyncMock(return_value=OL_DOC)
        service.gb.search = AsyncMock(
            side_effect=httpx.HTTPStatusError("500", request=None, response=None)
        )

        results = await service.enrich([CANDIDATE])

        assert len(results) == 1
        book = results[0]
        assert book.title == "Dune"
        assert book.open_library_work_id == "OL45804W"  # OL succeeded
        assert book.google_books_id is None  # GB failed — empty dict
        assert book.description is None  # no GB data
        assert "Science fiction" in book.subjects  # OL subjects present

    async def test_handles_both_apis_failing(self, service):
        """Both APIs raise → result uses only candidate data, no enrichment."""
        service.ol.search = AsyncMock(
            side_effect=httpx.TimeoutException("OL timed out")
        )
        service.gb.search = AsyncMock(
            side_effect=httpx.ConnectError("GB connection refused")
        )

        results = await service.enrich([CANDIDATE])

        assert len(results) == 1
        book = results[0]
        assert book.title == "Dune"
        assert book.author == "Frank Herbert"
        assert book.open_library_work_id is None
        assert book.google_books_id is None
        assert book.description is None
        assert book.subjects == []
        assert book.confidence == 0.97

    async def test_handles_ol_timeout_specifically(self, service):
        """OL times out; GB succeeds → partial enrichment with GB data."""
        service.ol.search = AsyncMock(
            side_effect=httpx.TimeoutException("OL timed out")
        )
        service.gb.search = AsyncMock(return_value=GB_VOLUME)

        results = await service.enrich([CANDIDATE])

        assert len(results) == 1
        book = results[0]
        assert book.open_library_work_id is None
        assert book.google_books_id == "gb_dune_001"
        assert book.editions[0].publisher == "Ace Books"

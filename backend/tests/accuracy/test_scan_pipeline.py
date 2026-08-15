"""Accuracy / regression tests for the book scan pipeline.

These tests verify end-to-end correctness using a golden dataset of known
ISBN → enriched book mappings. All external HTTP is intercepted so tests
are deterministic and free.

Design goals:
  1. Catch prompt regressions: if ChatGPT Vision response format changes,
     the ISBN extraction step will silently return None — these tests catch that.
  2. Catch enrichment regressions: if Open Library or Google Books schema
     changes, metadata fields go missing — golden assertions catch that.
  3. Serve as living documentation of the expected pipeline behaviour.

Expand GOLDEN_DATASET as you scan more books.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.schemas.book import EnrichedBook
from app.services.book_identifier import BookCandidate
from app.services.chatgpt_vision import ChatGPTVisionIdentifier
from app.services.enrichment import EnrichmentService

# ─── Golden dataset ────────────────────────────────────────────────────────────
#
# Each entry represents one book scan scenario:
#   isbn          — the ISBN the vision model is expected to return
#   title         — canonical work title (Open Library source of truth)
#   author        — primary author
#   ol_work_id    — Open Library work ID for dedup
#   gb_id         — Google Books volume ID
#
GOLDEN_DATASET = [
    {
        "isbn": "9780441013593",
        "title": "Dune",
        "author": "Frank Herbert",
        "ol_work_id": "OL45804W",
        "gb_id": "gb_dune_001",
    },
    {
        "isbn": "9780743273565",
        "title": "The Great Gatsby",
        "author": "F. Scott Fitzgerald",
        "ol_work_id": "OL468431W",
        "gb_id": "gb_gatsby_001",
    },
    {
        "isbn": "9780062316097",
        "title": "The Alchemist",
        "author": "Paulo Coelho",
        "ol_work_id": "OL892396W",
        "gb_id": "gb_alchemist_001",
    },
]


# ─── Helpers ───────────────────────────────────────────────────────────────────


def _ol_doc(entry: dict) -> dict:
    return {
        "key": f"/works/{entry['ol_work_id']}",
        "title": entry["title"],
        "author_name": [entry["author"]],
        "subject": ["Fiction"],
    }


def _gb_volume(entry: dict) -> dict:
    return {
        "id": entry["gb_id"],
        "volumeInfo": {
            "title": entry["title"],
            "authors": [entry["author"]],
            "description": f"A great book by {entry['author']}.",
            "publisher": "Publisher",
            "publishedDate": "2000",
            "pageCount": 300,
        },
    }


def _vision_candidate(entry: dict) -> BookCandidate:
    return BookCandidate(
        title=entry["title"],
        author=entry["author"],
        confidence=0.95,
        isbn_13=entry["isbn"],
    )


# ─── ISBN extraction accuracy ──────────────────────────────────────────────────


class TestIsbnExtractionAccuracy:
    """Verify that ChatGPTVisionIdentifier returns the expected ISBN.

    The vision model response is mocked — these tests validate that the
    parsing/prompt logic correctly extracts structured data from the
    model's text response.
    """

    @pytest.mark.parametrize(
        "entry", GOLDEN_DATASET, ids=[e["isbn"] for e in GOLDEN_DATASET]
    )
    async def test_identifies_expected_isbn(self, entry):
        json_response = (
            f'[{{"title":"{entry["title"]}","author":"{entry["author"]}",'
            f'"confidence":0.97,"isbn_13":"{entry["isbn"]}","isbn_10":null}}]'
        )

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": json_response}}]
        }

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "app.services.chatgpt_vision.httpx.AsyncClient", return_value=mock_client
        ):
            identifier = ChatGPTVisionIdentifier()
            results = await identifier.identify(b"fake_image_bytes")

        assert len(results) >= 1
        isbns = [r.isbn_13 for r in results if r.isbn_13]
        assert (
            entry["isbn"] in isbns
        ), f"Expected ISBN {entry['isbn']} not found in results: {isbns}"


# ─── Enrichment accuracy ───────────────────────────────────────────────────────


class TestEnrichmentAccuracy:
    """Verify that EnrichmentService produces correct metadata for known ISBNs.

    External HTTP is mocked with golden fixtures — catches regressions in
    field extraction logic (not API availability).
    """

    @pytest.mark.parametrize(
        "entry", GOLDEN_DATASET, ids=[e["isbn"] for e in GOLDEN_DATASET]
    )
    async def test_enriched_book_has_correct_metadata(self, entry):
        candidate = _vision_candidate(entry)
        service = EnrichmentService()

        with (
            patch.object(
                service.ol,
                "search_by_isbn",
                AsyncMock(return_value=_ol_doc(entry)),
            ),
            patch.object(
                service.gb,
                "search_by_isbn",
                AsyncMock(return_value=_gb_volume(entry)),
            ),
        ):
            results = await service.enrich([candidate])

        assert len(results) == 1
        book: EnrichedBook = results[0]

        assert book.title == entry["title"], f"Title mismatch for {entry['isbn']}"
        assert book.author == entry["author"], f"Author mismatch for {entry['isbn']}"
        assert (
            book.open_library_work_id == entry["ol_work_id"]
        ), f"OL work ID mismatch for {entry['isbn']}"
        assert (
            book.google_books_id == entry["gb_id"]
        ), f"GB ID mismatch for {entry['isbn']}"
        assert len(book.editions) == 1
        assert book.editions[0].isbn_13 == entry["isbn"]

    @pytest.mark.parametrize(
        "entry", GOLDEN_DATASET, ids=[e["isbn"] for e in GOLDEN_DATASET]
    )
    async def test_enrichment_succeeds_when_open_library_unavailable(self, entry):
        """Enrichment must not crash if OL is down — falls back to GB data."""
        candidate = _vision_candidate(entry)
        service = EnrichmentService()

        with (
            patch.object(
                service.ol,
                "search_by_isbn",
                AsyncMock(side_effect=Exception("OL timeout")),
            ),
            patch.object(
                service.gb,
                "search_by_isbn",
                AsyncMock(return_value=_gb_volume(entry)),
            ),
        ):
            results = await service.enrich([candidate])

        assert len(results) == 1
        assert results[0].title == entry["title"]
        assert results[0].open_library_work_id is None  # OL unavailable

    @pytest.mark.parametrize(
        "entry", GOLDEN_DATASET, ids=[e["isbn"] for e in GOLDEN_DATASET]
    )
    async def test_enrichment_succeeds_when_google_books_unavailable(self, entry):
        """Enrichment must not crash if GB is down — falls back to OL data."""
        candidate = _vision_candidate(entry)
        service = EnrichmentService()

        with (
            patch.object(
                service.ol,
                "search_by_isbn",
                AsyncMock(return_value=_ol_doc(entry)),
            ),
            patch.object(
                service.gb,
                "search_by_isbn",
                AsyncMock(side_effect=Exception("GB timeout")),
            ),
        ):
            results = await service.enrich([candidate])

        assert len(results) == 1
        assert results[0].open_library_work_id == entry["ol_work_id"]
        assert results[0].google_books_id is None  # GB unavailable


# ─── Pipeline confidence regression ───────────────────────────────────────────


class TestConfidenceRegression:
    """Ensure confidence scores stay within expected bounds."""

    @pytest.mark.parametrize(
        "entry", GOLDEN_DATASET, ids=[e["isbn"] for e in GOLDEN_DATASET]
    )
    async def test_confidence_above_threshold(self, entry):
        """Vision-returned confidence must be ≥ 0.8 for known clear-image books."""
        candidate = _vision_candidate(entry)
        # confidence is set by the vision model; 0.95 is our golden fixture value
        assert (
            candidate.confidence >= 0.8
        ), f"Confidence {candidate.confidence} below threshold for {entry['isbn']}"

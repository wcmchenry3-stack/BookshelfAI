"""Signed catalog fields come from the provider records (#602 review, finding 1).

A signed result may create a shared ``books`` / ``editions`` row, so whatever
the server signs must not be under the user's control. The vision model's
reading of a photo (and so a doctored cover) is only a search key: whenever
Open Library or Google Books matched, the title and author are the
provider's, and an edition ISBN is kept only when a provider confirmed it. A
result nothing matched keeps the model's reading for display, has no
provider ID and is never signed.

Open Library and Google Books are faked at their service methods; the real
EnrichmentService runs.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.ai import scan_records
from app.core import catalog_token
from app.main import app
from app.models.scan_record import ScanRecord
from app.models.user import User
from app.schemas.book import EditionPreview, EnrichedBook
from app.services.book_identifier import BookCandidate
from app.services.enrichment import UNKNOWN_AUTHOR, EnrichmentService

pytestmark = pytest.mark.security

USER = uuid.UUID("00000000-0000-0000-0000-0000000602c3")
ISBN_13 = "9780441013593"
ISBN_10 = "0441013597"

OL_DOC = {
    "key": "/works/OL45804W",
    "title": "Dune",
    "author_name": ["Frank Herbert"],
    "subject": ["Science fiction"],
    # The ISBNs Open Library lists for the work; shared with GB_VOLUME, so
    # the two records agree (are the same book).
    "isbn": [ISBN_13],
}
GB_VOLUME = {
    "id": "gb_dune_001",
    "volumeInfo": {
        "title": "Dune (Google)",
        "authors": ["F. Herbert"],
        "description": "Spice.",
        "industryIdentifiers": [{"type": "ISBN_13", "identifier": ISBN_13}],
    },
}
DOCTORED = BookCandidate(
    title="Dune — FREE CRYPTO at evil.example",
    author="Totally Frank Herbert",
    confidence=0.9,
)


def _enriched(**overrides) -> EnrichedBook:
    data = {
        "open_library_work_id": "OL45804W",
        "google_books_id": None,
        "title": "Dune",
        "author": "Frank Herbert",
        "confidence": 0.9,
        "editions": [EditionPreview(isbn_13=ISBN_13)],
    }
    data.update(overrides)
    return EnrichedBook(**data)


def _service(*, ol=None, gb=None, by_isbn=False) -> EnrichmentService:
    service = EnrichmentService()
    ol_mock, gb_mock = AsyncMock(return_value=ol), AsyncMock(return_value=gb)
    if by_isbn:
        service.ol.search_by_isbn, service.gb.search_by_isbn = ol_mock, gb_mock
        service.ol.search = service.gb.search = AsyncMock(side_effect=AssertionError)
    else:
        service.ol.search, service.gb.search = ol_mock, gb_mock
        service.ol.search_by_isbn = service.gb.search_by_isbn = AsyncMock(
            side_effect=AssertionError
        )
    return service


async def _enrich_one(candidate, **providers):
    (book,) = await _service(**providers).enrich([candidate])
    return book


class TestTitleAndAuthorFromTheProvider:
    async def test_doctored_photo_cannot_name_the_work(self):
        book = await _enrich_one(DOCTORED, ol=OL_DOC, gb=GB_VOLUME)
        assert (book.title, book.author) == ("Dune", "Frank Herbert")
        assert book.open_library_work_id == "OL45804W"
        # What is signed (and so what POST /wishlist can create) is the
        # provider's title, never the photo's.
        (signed,) = catalog_token.sign_books([book], USER)
        forged = signed.model_copy(update={"title": DOCTORED.title})
        assert catalog_token.verify(signed.catalog_token, signed, USER)
        assert not catalog_token.verify(signed.catalog_token, forged, USER)

    async def test_google_books_when_open_library_missed(self):
        book = await _enrich_one(DOCTORED, ol=None, gb=GB_VOLUME)
        assert (book.title, book.author) == ("Dune (Google)", "F. Herbert")
        assert book.google_books_id == "gb_dune_001"

    async def test_author_from_google_books_when_open_library_has_none(self):
        doc = {k: v for k, v in OL_DOC.items() if k != "author_name"}
        book = await _enrich_one(DOCTORED, ol=doc, gb=GB_VOLUME)
        assert (book.title, book.author) == ("Dune", "F. Herbert")

    async def test_no_provider_author_is_unknown_never_the_photo(self):
        doc = {k: v for k, v in OL_DOC.items() if k != "author_name"}
        book = await _enrich_one(DOCTORED, ol=doc, gb=None)
        assert book.author == UNKNOWN_AUTHOR

    @pytest.mark.parametrize("title", [None, "", "   ", 7, "x" * 501])
    async def test_a_record_without_a_usable_title_is_not_a_match(self, title):
        doc = {**OL_DOC, "title": title}
        volume = {**GB_VOLUME, "volumeInfo": {**GB_VOLUME["volumeInfo"]}}
        volume["volumeInfo"]["title"] = title
        book = await _enrich_one(DOCTORED, ol=doc, gb=volume)
        # Nothing from those records is paired with the photo's reading.
        assert book.open_library_work_id is None and book.google_books_id is None
        assert book.subjects == [] and book.description is None
        (book,) = catalog_token.sign_books([book], USER)
        assert book.catalog_token is None

    async def test_nothing_matched_shows_the_reading_unsigned(self):
        candidate = BookCandidate(
            title="Self-published zine", author="Me", confidence=0.4, isbn_13=ISBN_13
        )
        book = await _enrich_one(candidate, ol=None, gb=None, by_isbn=True)
        assert (book.title, book.author) == ("Self-published zine", "Me")
        assert book.open_library_work_id is None and book.google_books_id is None
        assert book.editions[0].isbn_13 is None  # read by the model only
        (book,) = catalog_token.sign_books([book], USER)
        assert book.catalog_token is None


class TestOnlyConfirmedIsbns:
    async def test_isbn_looked_up_and_listed_is_kept(self):
        candidate = BookCandidate(
            title="Dune", author="Frank Herbert", confidence=0.9, isbn_13=ISBN_13
        )
        book = await _enrich_one(candidate, ol=OL_DOC, gb=None, by_isbn=True)
        assert book.editions[0].isbn_13 == ISBN_13

    async def test_isbn_10_only_the_model_read_is_dropped(self):
        candidate = BookCandidate(
            title="Dune",
            author="Frank Herbert",
            confidence=0.9,
            isbn_13=ISBN_13,
            isbn_10="0000000000",
        )
        book = await _enrich_one(candidate, ol=OL_DOC, gb=GB_VOLUME, by_isbn=True)
        assert book.editions[0].isbn_13 == ISBN_13
        assert book.editions[0].isbn_10 is None

    async def test_isbns_listed_by_the_provider_are_kept(self):
        candidate = BookCandidate(
            title="Dune", author="Frank Herbert", confidence=0.9, isbn_10=ISBN_10
        )
        doc = {**OL_DOC, "isbn": [ISBN_10, ISBN_13]}
        book = await _enrich_one(candidate, ol=doc, gb=None)
        assert book.editions[0].isbn_10 == ISBN_10

    async def test_title_search_isbn_10_not_listed_is_dropped(self):
        candidate = BookCandidate(
            title="Dune", author="Frank Herbert", confidence=0.9, isbn_10=ISBN_10
        )
        book = await _enrich_one(candidate, ol=OL_DOC, gb=GB_VOLUME)
        assert book.editions[0].isbn_10 is None

    async def test_google_books_identifiers_confirm(self):
        candidate = BookCandidate(
            title="Dune", author="Frank Herbert", confidence=0.9, isbn_10=ISBN_10
        )
        volume = {**GB_VOLUME, "volumeInfo": {**GB_VOLUME["volumeInfo"]}}
        volume["volumeInfo"]["industryIdentifiers"] = [
            {"type": "ISBN_10", "identifier": ISBN_10},
            {"type": "OTHER"},
            "junk",
        ]
        book = await _enrich_one(candidate, ol=None, gb=volume)
        assert book.editions[0].isbn_10 == ISBN_10


class TestIsbn13ConfirmedOnlyByIdentifiers:
    """A lookup by ISBN X that returned a record does not confirm X: Google
    Books ``q=isbn:X`` is a keyword search and may return a volume that does
    not list X (#602 security review round 2, finding 2)."""

    CANDIDATE = BookCandidate(
        title="Dune", author="Frank Herbert", confidence=0.9, isbn_13=ISBN_13
    )

    async def test_google_books_volume_not_listing_it(self):
        volume = {**GB_VOLUME, "volumeInfo": {**GB_VOLUME["volumeInfo"]}}
        volume["volumeInfo"]["industryIdentifiers"] = [
            {"type": "ISBN_13", "identifier": "9780000000002"}
        ]
        book = await _enrich_one(self.CANDIDATE, ol=None, gb=volume, by_isbn=True)
        assert book.google_books_id == "gb_dune_001"
        assert book.editions[0].isbn_13 is None
        (signed,) = catalog_token.sign_books([book], USER)
        assert signed.editions[0].isbn_13 is None

    async def test_open_library_doc_not_listing_it(self):
        doc = {**OL_DOC, "isbn": ["9780000000002"]}
        book = await _enrich_one(self.CANDIDATE, ol=doc, gb=None, by_isbn=True)
        assert book.open_library_work_id == "OL45804W"
        assert book.editions[0].isbn_13 is None

    async def test_unsigned_isbn_cannot_create_an_edition(self):
        volume = {**GB_VOLUME, "volumeInfo": {**GB_VOLUME["volumeInfo"]}}
        del volume["volumeInfo"]["industryIdentifiers"]
        book = await _enrich_one(self.CANDIDATE, ol=None, gb=volume, by_isbn=True)
        (signed,) = catalog_token.sign_books([book], USER)
        # Adding the ISBN back breaks the signature.
        forged = signed.model_copy(
            update={"editions": [EditionPreview(isbn_13=ISBN_13)]}
        )
        assert catalog_token.verify(signed.catalog_token, signed, USER)
        assert not catalog_token.verify(signed.catalog_token, forged, USER)


NEUROMANCER_VOLUME = {
    "id": "gb_neuromancer",
    "volumeInfo": {
        "title": "Neuromancer",
        "authors": ["William Gibson"],
        "description": "Cyberspace.",
        "publisher": "Ace",
        "publishedDate": "1984",
        "pageCount": 271,
        "imageLinks": {"thumbnail": "http://books.google.com/n.jpg"},
        "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9780441569595"}],
    },
}


class TestProvidersMustAgree:
    """Open Library and Google Books may match different books; the volume
    is used only when it is the same book as the Open Library doc (#602
    security review round 2, finding 3)."""

    async def test_disagreeing_volume_is_dropped_entirely(self):
        candidate = BookCandidate(title="Dune", author="Herbert", confidence=0.9)
        book = await _enrich_one(candidate, ol=OL_DOC, gb=NEUROMANCER_VOLUME)
        assert (book.title, book.author) == ("Dune", "Frank Herbert")
        assert book.open_library_work_id == "OL45804W"
        assert book.google_books_id is None
        assert book.description is None and book.cover_url is None
        (edition,) = book.editions
        assert (edition.publisher, edition.publish_year, edition.page_count) == (
            None,
            None,
            None,
        )
        assert book.subjects == ["Science fiction"]

    async def test_disagreeing_volume_confirms_no_isbn(self):
        candidate = BookCandidate(
            title="Dune",
            author="Herbert",
            confidence=0.9,
            isbn_13="9780441569595",
        )
        doc = {k: v for k, v in OL_DOC.items() if k != "isbn"}
        book = await _enrich_one(candidate, ol=doc, gb=NEUROMANCER_VOLUME, by_isbn=True)
        assert book.google_books_id is None
        assert book.editions[0].isbn_13 is None

    async def test_agree_by_shared_isbn(self):
        volume = {**GB_VOLUME, "volumeInfo": {**GB_VOLUME["volumeInfo"]}}
        volume["volumeInfo"]["title"] = "Dune: Deluxe Edition"
        book = await _enrich_one(DOCTORED, ol=OL_DOC, gb=volume)
        assert book.google_books_id == "gb_dune_001"
        assert book.description == "Spice."

    async def test_agree_by_title_and_author(self):
        doc = {
            "key": "/works/OL59863W",
            "title": "The Left Hand of Darkness",
            "author_name": ["Ursula K. Le Guin"],
        }
        volume = {
            "id": "gb_lhod",
            "volumeInfo": {
                "title": "Left Hand of Darkness!",
                "authors": ["Ursula K Le  Guin"],
                "description": "Gethen.",
            },
        }
        candidate = BookCandidate(
            title="Left Hand of Darkness", author="Le Guin", confidence=0.9
        )
        book = await _enrich_one(candidate, ol=doc, gb=volume)
        assert book.title == "The Left Hand of Darkness"
        assert (book.google_books_id, book.description) == ("gb_lhod", "Gethen.")

    async def test_google_books_alone_is_still_used(self):
        candidate = BookCandidate(title="Neuromancer", author="Gibson", confidence=0.9)
        book = await _enrich_one(candidate, ol=None, gb=NEUROMANCER_VOLUME)
        assert book.google_books_id == "gb_neuromancer"
        assert book.title == "Neuromancer"


class TestIsbn10PairsWithTheIsbn13:
    """An Open Library work lists every edition's ISBNs, so a listed ISBN-10
    may be another edition's; beside a confirmed ISBN-13 only its own
    ISBN-10 form is kept (#602 security review round 2, finding 4)."""

    OTHER_EDITION_10 = "0340960191"

    async def test_other_editions_isbn_10_is_dropped(self):
        candidate = BookCandidate(
            title="Dune",
            author="Frank Herbert",
            confidence=0.9,
            isbn_13=ISBN_13,
            isbn_10=self.OTHER_EDITION_10,
        )
        doc = {**OL_DOC, "isbn": [ISBN_13, self.OTHER_EDITION_10]}
        book = await _enrich_one(candidate, ol=doc, gb=None, by_isbn=True)
        assert book.editions[0].isbn_13 == ISBN_13
        assert book.editions[0].isbn_10 is None

    async def test_matching_isbn_10_is_kept(self):
        candidate = BookCandidate(
            title="Dune",
            author="Frank Herbert",
            confidence=0.9,
            isbn_13=ISBN_13,
            isbn_10=ISBN_10,
        )
        book = await _enrich_one(candidate, ol=OL_DOC, gb=GB_VOLUME, by_isbn=True)
        assert (book.editions[0].isbn_13, book.editions[0].isbn_10) == (
            ISBN_13,
            ISBN_10,
        )

    async def test_unconfirmed_isbn_13_keeps_a_listed_isbn_10_alone(self):
        candidate = BookCandidate(
            title="Dune",
            author="Frank Herbert",
            confidence=0.9,
            isbn_13="9780000000002",
            isbn_10=self.OTHER_EDITION_10,
        )
        doc = {**OL_DOC, "isbn": [self.OTHER_EDITION_10]}
        book = await _enrich_one(candidate, ol=doc, gb=None, by_isbn=True)
        assert (book.editions[0].isbn_13, book.editions[0].isbn_10) == (
            None,
            self.OTHER_EDITION_10,
        )


class TestSearchSignsOnlyProviderData:
    """/books/search builds its candidates from Google Books volumes; the
    same enrichment rules decide what it signs."""

    def _search(self, volumes, *, ol=None, gb=None):
        from app.api import books as books_api
        from app.auth.dependencies import get_current_user

        app.dependency_overrides[get_current_user] = lambda: User(
            id=USER, email="s@x.io"
        )
        enrichment = books_api._enrichment
        try:
            with (
                patch.object(
                    books_api._gb, "search_query", AsyncMock(return_value=volumes)
                ),
                patch.object(enrichment.ol, "search", AsyncMock(return_value=ol)),
                patch.object(enrichment.gb, "search", AsyncMock(return_value=gb)),
                patch.object(
                    enrichment.ol, "search_by_isbn", AsyncMock(return_value=ol)
                ),
                patch.object(
                    enrichment.gb, "search_by_isbn", AsyncMock(return_value=gb)
                ),
            ):
                resp = TestClient(app).get("/books/search?q=dune")
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code == 200, resp.text
        return resp.json()

    def test_matched_result_is_signed_with_provider_fields(self):
        (book,) = self._search([GB_VOLUME], ol=OL_DOC, gb=GB_VOLUME)
        assert (book["title"], book["author"]) == ("Dune", "Frank Herbert")
        assert book["editions"][0]["isbn_13"] == ISBN_13
        assert book["catalog_token"]

    def test_unmatched_result_is_not_signed(self):
        (book,) = self._search([GB_VOLUME], ol=None, gb=None)
        assert book["open_library_work_id"] is None
        assert book["google_books_id"] is None
        assert book["catalog_token"] is None


class TestScanRecordsFromBeforeTheFix:
    """Books saved before catalog fields came only from the providers may
    hold the model's title / ISBNs; a repeat must redo the enrichment step
    rather than sign them."""

    def _legacy_item(self):
        (item,) = scan_records.serialise_books([_enriched(title=DOCTORED.title)])
        item.pop(scan_records.BOOK_FORMAT_KEY)
        return item

    def test_saved_books_carry_the_format_marker(self):
        (item,) = scan_records.serialise_books([_enriched()])
        assert item[scan_records.BOOK_FORMAT_KEY] == scan_records.BOOK_FORMAT_VERSION

    @pytest.mark.parametrize("marker", [None, 1, True, "2", 3])
    def test_old_or_wrong_marker_is_rejected(self, marker):
        item = self._legacy_item()
        if marker is not None:
            item[scan_records.BOOK_FORMAT_KEY] = marker
        with pytest.raises(scan_records.ScanRecordError):
            scan_records.deserialise_books([item])

    async def test_old_enrichment_is_a_missing_step(self):
        row = ScanRecord(
            user_id=USER,
            fingerprint="c" * 64,
            feature="scan.standard",
            tier="free",
            model="gpt-4o-mini",
            candidates=scan_records.serialise_candidates([DOCTORED]),
            enrichment=[self._legacy_item()],
            ai_done=True,
            enrich_done=True,
            created_at=datetime.now(UTC),
        )
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = row
        db.execute = AsyncMock(return_value=result)
        record = await scan_records.lookup(
            db, user_id=USER, fingerprint="c" * 64, retention_days=30
        )
        assert record is not None and record.candidates == [DOCTORED]
        assert record.books is None  # the repeat re-enriches

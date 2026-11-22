"""A subtitle reading that finds nothing is searched again by its main title
(#1003). Providers often keep the subtitle in a separate field, so the full
title matches nothing. Provider HTTP is faked at ``get_json``; the real
services and scorer run. No network, no AI."""

from unittest.mock import patch

from app.services.book_identifier import BookCandidate
from app.services.enrichment import EnrichmentService

OL_PATH = "app.services.open_library.get_json"
GB_PATH = "app.services.google_books.get_json"


def _ol(title, subtitle, author):
    return {
        "docs": [
            {
                "key": "/works/OL1W",
                "title": title,
                "subtitle": subtitle,
                "author_name": [author],
            }
        ]
    }


def _gb(title, subtitle, author):
    return {
        "items": [
            {
                "id": "gb1",
                "volumeInfo": {
                    "title": title,
                    "subtitle": subtitle,
                    "authors": [author],
                },
            }
        ]
    }


def _fakes(ol_rec, gb_rec, *, full_miss=True, exc=None):
    """Fake get_json pair. A search for the full title (it contains ':')
    answers with nothing (or raises ``exc``); a search by the bare main title
    answers with the record."""
    log = {"ol": [], "gb": []}

    async def ol(url, params):
        log["ol"].append(params["title"])
        if ":" in params["title"]:  # the full title
            if exc:
                raise exc
            if full_miss:
                return {"docs": []}
        return ol_rec

    async def gb(url, params):
        log["gb"].append(params["q"])
        if ":" in params["q"].split("inauthor")[0].partition(":")[2].strip('" '):
            if exc:
                raise exc
            if full_miss:
                return {"items": []}
        return gb_rec

    return ol, gb, log


async def _run(candidate, ol, gb):
    service = EnrichmentService()
    with patch(OL_PATH, ol), patch(GB_PATH, gb):
        books = await service.enrich([candidate])
    return books[0], service


def _washington():
    return BookCandidate(
        title="Washington: A Life", author="Ron Chernow", confidence=0.9
    )


class TestSubtitleFallback:
    async def test_main_title_search_matches_both_providers(self):
        ol, gb, log = _fakes(
            _ol("Washington", "A Life", "Ron Chernow"),
            _gb("Washington", "A Life", "Ron Chernow"),
        )
        book, service = await _run(_washington(), ol, gb)
        assert book.open_library_work_id == "OL1W"
        assert book.google_books_id == "gb1"
        # Full title, then the main title alone: exactly two per provider.
        assert len(log["ol"]) == 2 and len(log["gb"]) == 2
        assert log["ol"][1] == "washington"
        assert "washington" in log["gb"][1] and "A Life" not in log["gb"][1]
        assert service.last_complete is True

    async def test_no_subtitle_makes_no_extra_request(self):
        log: list[str] = []

        async def ol(url, params):
            log.append("ol")
            return {"docs": []}

        async def gb(url, params):
            log.append("gb")
            return {"items": []}

        candidate = BookCandidate(
            title="Washington", author="Ron Chernow", confidence=0.9
        )
        book, _ = await _run(candidate, ol, gb)
        assert sorted(log) == ["gb", "ol"]
        assert book.open_library_work_id is None

    async def test_nothing_fits_costs_one_extra_request_per_provider(self):
        log: list[str] = []

        async def ol(url, params):
            log.append("ol")
            return {"docs": []}

        async def gb(url, params):
            log.append("gb")
            return {"items": []}

        await _run(_washington(), ol, gb)
        assert sorted(log) == ["gb", "gb", "ol", "ol"]

    async def test_a_fitting_full_title_search_is_not_repeated(self):
        ol, gb, log = _fakes(
            _ol("Washington", "A Life", "Ron Chernow"),
            _gb("Washington", "A Life", "Ron Chernow"),
            full_miss=False,
        )
        book, _ = await _run(_washington(), ol, gb)
        assert len(log["ol"]) == 1 and len(log["gb"]) == 1
        assert book.open_library_work_id == "OL1W"

    async def test_saga_book_two_never_matches_deluxe_edition(self):
        ol, gb, log = _fakes(
            _ol("Saga", "Book Three", "Brian K. Vaughan"),
            _gb("Saga", "Book Three", "Brian K. Vaughan"),
        )
        candidate = BookCandidate(
            title="Saga: Book Two", author="Brian K. Vaughan", confidence=0.9
        )
        book, _ = await _run(candidate, ol, gb)
        assert len(log["ol"]) == 2  # the fallback ran; the scorer refused
        assert book.open_library_work_id is None
        assert book.google_books_id is None

    async def test_deluxe_edition_note_is_not_a_volume_match_for_book_two(self):
        # The record's "Deluxe Edition" is an edition note, but the reading
        # names a volume; the same-subtitle rule is unchanged by the fallback.
        ol, gb, _ = _fakes(
            _ol("Saga", "Deluxe Edition", "Brian K. Vaughan"),
            _gb("Saga", "Deluxe Edition", "Brian K. Vaughan"),
        )
        candidate = BookCandidate(
            title="Saga: Book Two", author="Brian K. Vaughan", confidence=0.9
        )
        book, _ = await _run(candidate, ol, gb)
        assert book.open_library_work_id is None
        assert book.google_books_id is None

    async def test_other_author_with_same_surname_never_matches(self):
        ol, gb, _ = _fakes(
            _ol("Dune", "Messiah", "Brian Herbert"),
            _gb("Dune", "Messiah", "Brian Herbert"),
        )
        candidate = BookCandidate(
            title="Dune: Messiah", author="Frank Herbert", confidence=0.9
        )
        book, _ = await _run(candidate, ol, gb)
        assert book.open_library_work_id is None
        assert book.google_books_id is None

    async def test_a_failed_lookup_is_not_retried(self):
        ol, gb, log = _fakes(
            _ol("Washington", "A Life", "Ron Chernow"),
            _gb("Washington", "A Life", "Ron Chernow"),
            exc=ConnectionError("down"),
        )
        book, service = await _run(_washington(), ol, gb)
        assert len(log["ol"]) == 1 and len(log["gb"]) == 1
        assert book.open_library_work_id is None
        assert service.last_complete is False

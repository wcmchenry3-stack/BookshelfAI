"""A subtitle reading that finds nothing is searched again by its main title
(#1003). Providers often keep the subtitle in a separate field, so the full
title matches nothing. Provider HTTP is faked at ``get_json``; the real
services and scorer run. No network, no AI."""

import asyncio
from unittest.mock import patch

from app.services.book_identifier import BookCandidate
from app.services.enrichment import EnrichmentService
from app.services.enrichment import service as service_module
from app.services.enrichment.scoring import _ol_parts, _scorer
from app.services.enrichment.service import FALLBACK_MIN_SCORE

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


def _is_full_title_search(text):
    """True when the search text still carries a subtitle separator."""
    return ":" in text.split("inauthor")[0].replace("intitle:", "")


def _fakes(ol_rec, gb_rec, *, full_miss=True, exc=None):
    """Fake get_json pair. A search for the full title answers with nothing
    (or raises ``exc``); a search by the bare main title answers with the
    record."""
    log = {"ol": [], "gb": []}

    async def ol(url, params):
        log["ol"].append(params["title"])
        if _is_full_title_search(params["title"]):
            if exc:
                raise exc
            if full_miss:
                return {"docs": []}
        return ol_rec

    async def gb(url, params):
        log["gb"].append(params["q"])
        if _is_full_title_search(params["q"]):
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


def _reading(title, author):
    return BookCandidate(title=title, author=author, confidence=0.9)


def _score(candidate, title, subtitle, author):
    return _scorer(candidate, _ol_parts)(
        {"title": title, "subtitle": subtitle, "author_name": [author]}
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
        assert log["ol"][1] == "Washington"
        assert '"Washington"' in log["gb"][1] and "A Life" not in log["gb"][1]
        assert service.last_complete is True

    async def test_the_issue_case_clears_the_fallback_bar(self):
        score = _score(_washington(), "Washington", "A Life", "Ron Chernow")
        assert score >= FALLBACK_MIN_SCORE

    async def test_no_subtitle_makes_no_extra_request(self):
        log: list[str] = []

        async def ol(url, params):
            log.append("ol")
            return {"docs": []}

        async def gb(url, params):
            log.append("gb")
            return {"items": []}

        book, _ = await _run(_reading("Washington", "Ron Chernow"), ol, gb)
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


class TestScorerStillDecides:
    async def test_saga_book_two_never_matches_book_three(self):
        ol, gb, log = _fakes(
            _ol("Saga", "Book Three", "Brian K. Vaughan"),
            _gb("Saga", "Book Three", "Brian K. Vaughan"),
        )
        book, _ = await _run(_reading("Saga: Book Two", "Brian K. Vaughan"), ol, gb)
        assert len(log["ol"]) == 2 and len(log["gb"]) == 2  # the fallback ran
        assert book.open_library_work_id is None
        assert book.google_books_id is None

    async def test_saga_book_two_never_matches_deluxe_edition(self):
        ol, gb, log = _fakes(
            _ol("Saga", "Deluxe Edition", "Brian K. Vaughan"),
            _gb("Saga", "Deluxe Edition", "Brian K. Vaughan"),
        )
        book, _ = await _run(_reading("Saga: Book Two", "Brian K. Vaughan"), ol, gb)
        assert len(log["ol"]) == 2 and len(log["gb"]) == 2
        assert book.open_library_work_id is None
        assert book.google_books_id is None

    async def test_other_author_with_same_surname_never_matches(self):
        ol, gb, log = _fakes(
            _ol("Dune", "Messiah", "Brian Herbert"),
            _gb("Dune", "Messiah", "Brian Herbert"),
        )
        book, _ = await _run(_reading("Dune: Messiah", "Frank Herbert"), ol, gb)
        assert len(log["ol"]) == 2 and len(log["gb"]) == 2
        assert book.open_library_work_id is None
        assert book.google_books_id is None


class TestFallbackNeedsAgreeingTitles:
    """A bare series name with the same surname brings back siblings and
    omnibus editions. They score 2 (one-sided subtitle), below the fallback's
    bar, so they are never signed into the shared catalog."""

    async def _miss_then_return(self, reading, title, subtitle, rec_author):
        ol, gb, log = _fakes(
            _ol(title, subtitle, rec_author), _gb(title, subtitle, rec_author)
        )
        book, _ = await _run(reading, ol, gb)
        assert len(log["ol"]) == 2 and len(log["gb"]) == 2  # the fallback ran
        return book

    async def test_dune_messiah_never_matches_dune(self):
        reading = _reading("Dune: Messiah", "Frank Herbert")
        assert _score(reading, "Dune", None, "Frank Herbert") == 2
        book = await self._miss_then_return(reading, "Dune", None, "Frank Herbert")
        assert book.open_library_work_id is None and book.google_books_id is None

    async def test_two_towers_never_matches_the_omnibus(self):
        reading = _reading("The Lord of the Rings: The Two Towers", "J.R.R. Tolkien")
        assert _score(reading, "The Lord of the Rings", None, "J. R. R. Tolkien") == 2
        book = await self._miss_then_return(
            reading, "The Lord of the Rings", None, "J. R. R. Tolkien"
        )
        assert book.open_library_work_id is None and book.google_books_id is None

    async def test_a_record_without_the_subtitle_is_rejected(self):
        reading = _washington()
        assert _score(reading, "Washington", None, "Ron Chernow") == 2
        book = await self._miss_then_return(reading, "Washington", None, "Ron Chernow")
        assert book.open_library_work_id is None and book.google_books_id is None


class TestFallbackQuery:
    async def test_raw_text_is_sent(self):
        ol, gb, log = _fakes(
            _ol("Don't Panic", "Why", "A B"), _gb("Don't Panic", "Why", "A B")
        )
        await _run(_reading("Don't Panic: Why", "A B"), ol, gb)
        assert log["ol"][1] == "Don't Panic"

    async def test_a_main_title_under_four_characters_is_not_searched(self):
        for title in ("A: Book Of Things", "Go: A Story", "Why: Not Really"):
            log: list[str] = []

            async def ol(url, params, log=log):
                log.append("ol")
                return {"docs": []}

            async def gb(url, params, log=log):
                log.append("gb")
                return {"items": []}

            await _run(_reading(title, "X Y"), ol, gb)
            assert sorted(log) == ["gb", "ol"], title

    async def test_remaining_time_arithmetic(self):
        loop = asyncio.get_running_loop()
        timeout = service_module.PROVIDER_CALL_TIMEOUT_S
        for left, expected in ((timeout - 1, None), (timeout + 1, "Washington")):
            token = service_module._DEADLINE_AT.set(loop.time() + left)
            try:
                assert service_module._fallback_query("Washington: A Life") == expected
            finally:
                service_module._DEADLINE_AT.reset(token)

    async def test_no_fallback_with_less_than_one_call_timeout_left(self):
        ol, gb, log = _fakes(
            _ol("Washington", "A Life", "Ron Chernow"),
            _gb("Washington", "A Life", "Ron Chernow"),
        )
        with patch.object(service_module, "ENRICHMENT_DEADLINE_S", 5.0):
            book, _ = await _run(_washington(), ol, gb)
        assert len(log["ol"]) == 1 and len(log["gb"]) == 1
        assert book.open_library_work_id is None


class TestFailureAndPartial:
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

    async def test_a_fallback_that_raises_is_a_lookup_failure(self):
        log: list[str] = []

        async def ol(url, params):
            log.append("ol")
            if len(log) > 1:
                raise ConnectionError("down")
            return {"docs": []}

        async def gb(url, params):
            return _gb("Washington", "A Life", "Ron Chernow")

        book, service = await _run(_washington(), ol, gb)
        assert log == ["ol", "ol"]
        assert book.open_library_work_id is None
        assert service.last_complete is False

    async def test_a_fallback_on_one_provider_only(self):
        gb_log: list[str] = []
        ol_log: list[str] = []

        async def ol(url, params):
            ol_log.append(params["title"])
            return {"docs": []}

        async def gb(url, params):
            gb_log.append(params["q"])
            return _gb("Washington", "A Life", "Ron Chernow")

        book, _ = await _run(_washington(), ol, gb)
        assert len(gb_log) == 1  # fits on the first search: no extra request
        assert len(ol_log) == 2  # Open Library ran its fallback
        assert book.google_books_id == "gb1"


VALID_ISBN = "9780306406157"


class TestWeakIsbnRecordAndFallback:
    """A weak ISBN record (bare "Washington", score 2, not listing the ISBN)
    is kept only if the fallback finds nothing better."""

    async def _run_with_fallback(self, fallback_subtitle):
        log = {"ol": [], "gb": []}
        weak_ol = _ol("Washington", None, "Ron Chernow")["docs"][0]
        weak_gb = _gb("Washington", None, "Ron Chernow")["items"][0]

        async def ol(url, params):
            log["ol"].append(params)
            if "isbn" in params:
                return {"docs": [weak_ol]}
            if ":" in params["title"]:
                return {"docs": []}
            rec = _ol("Washington", fallback_subtitle, "Ron Chernow")
            rec["docs"][0]["key"] = "/works/OL2W"
            return rec

        async def gb(url, params):
            log["gb"].append(params)
            if params["q"].startswith("isbn:"):
                return {"items": [weak_gb]}
            if _is_full_title_search(params["q"]):
                return {"items": []}
            rec = _gb("Washington", fallback_subtitle, "Ron Chernow")
            rec["items"][0]["id"] = "gb2"
            return rec

        candidate = BookCandidate(
            title="Washington: A Life",
            author="Ron Chernow",
            confidence=0.9,
            isbn_13=VALID_ISBN,
        )
        book, _ = await _run(candidate, ol, gb)
        assert len(log["ol"]) == 3 and len(log["gb"]) == 3
        return book

    async def test_a_better_fallback_record_replaces_the_weak_one(self):
        book = await self._run_with_fallback("A Life")
        assert (book.open_library_work_id, book.google_books_id) == ("OL2W", "gb2")
        assert book.title == "Washington"

    async def test_a_rejected_fallback_keeps_the_weak_record(self):
        book = await self._run_with_fallback("The Indispensable Man")
        # The weak record is the bare one (no subtitle); both providers kept it.
        assert (book.open_library_work_id, book.google_books_id) == ("OL1W", "gb1")

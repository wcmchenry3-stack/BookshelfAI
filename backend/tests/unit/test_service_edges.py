"""Small edge cases in services and core helpers: ISBN lookup, Sentry
scrubbing, title splitting, edition keys and cleanup-task isolation."""

import asyncio
import logging
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core import sentry_context
from app.services import deduplication, enrichment, token_cleanup
from app.services.google_books import GoogleBooksService


def _client(json_data):
    resp = MagicMock()
    resp.json.return_value = json_data
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


class TestSearchByIsbn:
    async def test_queries_by_isbn_and_returns_the_first_volume(self):
        client = _client({"items": [{"id": "a"}, {"id": "b"}]})
        with patch("app.services.google_books.httpx.AsyncClient", return_value=client):
            volume = await GoogleBooksService().search_by_isbn("9780441013593")
        assert volume == {"id": "a"}
        params = client.get.await_args.kwargs["params"]
        assert params["q"] == "isbn:9780441013593"
        assert params["maxResults"] == 1

    async def test_unknown_isbn_gives_none(self):
        with patch(
            "app.services.google_books.httpx.AsyncClient", return_value=_client({})
        ):
            assert await GoogleBooksService().search_by_isbn("0") is None

    async def test_api_key_is_sent_only_when_configured(self):
        client = _client({})
        with (
            patch("app.services.google_books.httpx.AsyncClient", return_value=client),
            patch("app.services.google_books.settings.google_books_api_key", "k-123"),
        ):
            await GoogleBooksService().search_by_isbn("1")
        assert client.get.await_args.kwargs["params"]["key"] == "k-123"

        client = _client({})
        with (
            patch("app.services.google_books.httpx.AsyncClient", return_value=client),
            patch("app.services.google_books.settings.google_books_api_key", ""),
        ):
            await GoogleBooksService().search_by_isbn("1")
        assert "key" not in client.get.await_args.kwargs["params"]


class TestSentryEventScrubbing:
    def test_request_query_string_is_filtered(self):
        event = {"request": {"url": "https://x/y", "query_string": "token=abc"}}
        out = sentry_context._scrub_event(event)
        assert out["request"]["query_string"] == sentry_context._FILTERED
        assert out["request"]["url"] == "https://x/y"

    def test_empty_query_string_is_left_alone(self):
        event = {"request": {"query_string": ""}}
        assert sentry_context._scrub_event(event)["request"]["query_string"] == ""

    def test_span_description_and_data_lose_their_query(self):
        event = {
            "spans": [
                {
                    "description": "GET https://api/x?key=s3cret",
                    "data": {
                        "url": "https://api/x?key=s3cret",
                        "http.query": "key=s3cret",
                    },
                }
            ]
        }
        span = sentry_context._scrub_event(event)["spans"][0]
        assert span["description"] == "GET https://api/x"
        assert span["data"]["url"] == "https://api/x"
        assert span["data"]["http.query"] == sentry_context._FILTERED

    @pytest.mark.parametrize("shape", ["list", "values"])
    def test_breadcrumbs_in_either_shape_are_scrubbed(self, shape):
        crumb = {"data": {"http.url": "https://api/x?token=abc"}}
        crumbs = [crumb] if shape == "list" else {"values": [crumb]}
        sentry_context._scrub_event({"breadcrumbs": crumbs})
        assert crumb["data"]["http.url"] == "https://api/x"

    def test_event_without_optional_sections_passes_through(self):
        assert sentry_context._scrub_event({"message": "hi"}) == {"message": "hi"}

    def test_non_dict_breadcrumb_data_is_ignored(self):
        crumb = {"data": "plain text"}
        assert sentry_context._scrub_breadcrumb(crumb) == crumb


class TestSplitTitle:
    def test_non_string_gives_empty_pair(self):
        assert enrichment._split_title(None) == ("", "")
        assert enrichment._split_title(42) == ("", "")

    def test_subtitle_is_split_off_and_normalised(self):
        main, sub = enrichment._split_title("Dune: The Graphic Novel")
        assert main == "dune"
        assert sub == "the graphic novel"

    def test_no_subtitle(self):
        main, sub = enrichment._split_title("Dune")
        assert (main, sub) == ("dune", "")


class TestEditionKey:
    def test_none_edition_has_no_key(self):
        assert deduplication.edition_key(None) is None

    def test_isbn_10_is_upgraded_to_the_same_key_as_isbn_13(self):
        by_13 = SimpleNamespace(isbn_13="9780441013593", isbn_10=None)
        by_10 = SimpleNamespace(isbn_13=None, isbn_10="0441013597")
        assert deduplication.edition_key(by_13) == "9780441013593"
        assert deduplication.edition_key(by_10) == "9780441013593"

    def test_edition_without_isbn_has_no_key(self):
        assert deduplication.edition_key(SimpleNamespace()) is None


class TestCleanupIsolation:
    """One failing purge must not stop the others, or the loop."""

    PURGES: ClassVar[list[str]] = [
        "purge_old_quota",
        "rollup_and_purge",
        "purge_old_spend",
        "purge_old_zero_results",
        "purge_old_scan_records",
    ]

    @staticmethod
    def _factory(execute):
        session = AsyncMock()
        session.execute = execute
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        return MagicMock(return_value=session)

    async def _run_once(self, patches, caplog):
        with caplog.at_level(logging.ERROR, logger="app.services.token_cleanup"):
            task_patches = [
                patch.object(token_cleanup, "_INITIAL_DELAY_SECONDS", 0),
                patch.object(token_cleanup, "_INTERVAL_SECONDS", 9999),
                *patches,
            ]
            for p in task_patches:
                p.start()
            try:
                task = asyncio.create_task(token_cleanup.cleanup_expired_tokens())
                for _ in range(20):
                    await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                for p in reversed(task_patches):
                    p.stop()

    async def test_every_purge_runs_even_when_each_one_fails(self, caplog):
        mocks = {
            name: AsyncMock(side_effect=RuntimeError(name)) for name in self.PURGES
        }
        patches = [patch.object(token_cleanup, n, m) for n, m in mocks.items()]
        patches.append(
            patch.object(
                token_cleanup,
                "AsyncSessionLocal",
                self._factory(AsyncMock(side_effect=RuntimeError("tokens"))),
            )
        )
        await self._run_once(patches, caplog)
        for name, mock in mocks.items():
            assert mock.await_count == 1, name
        # five purges plus the refresh-token delete each logged their failure.
        assert caplog.text.count("error during cleanup run") == len(self.PURGES) + 1

    async def test_refresh_token_failure_alone_is_logged_not_raised(self, caplog):
        result = MagicMock(rowcount=0)

        async def execute(stmt, *a, **k):
            if "refresh_tokens" in str(stmt):
                raise ValueError("lost")
            return result

        patches = [
            patch.object(token_cleanup, n, AsyncMock(return_value=0))
            for n in self.PURGES
        ]
        patches[2] = patch.object(
            token_cleanup, "rollup_and_purge", AsyncMock(return_value=(0, 0))
        )
        patches.append(
            patch.object(token_cleanup, "AsyncSessionLocal", self._factory(execute))
        )
        await self._run_once(patches, caplog)
        assert caplog.text.count("error during cleanup run") == 1

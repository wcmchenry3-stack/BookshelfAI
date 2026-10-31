"""The request and result helpers shared by the Google Books and Open
Library clients (#814, ``app/services/_lookup_http.py``). HTTP mocked."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services._lookup_http import (
    LOOKUP_TIMEOUT_S,
    dict_records,
    first_record,
    get_json,
)
from app.services.google_books import GOOGLE_BOOKS_URL, GoogleBooksService
from app.services.open_library import OpenLibraryService

PATCH = "app.services._lookup_http.httpx.AsyncClient"


def _client(json_data=None, *, status_error: Exception | None = None):
    resp = MagicMock()
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock(side_effect=status_error)
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


class TestGetJson:
    async def test_one_get_with_the_timeout_and_params(self):
        client = _client({"items": []})
        with patch(PATCH, return_value=client) as cls:
            data = await get_json("https://example.test/x", {"q": "Dune"})
        assert data == {"items": []}
        cls.assert_called_once_with(timeout=10)
        assert LOOKUP_TIMEOUT_S == 10
        client.get.assert_awaited_once_with(
            "https://example.test/x", params={"q": "Dune"}
        )

    async def test_a_bad_status_raises_before_reading_the_body(self):
        error = httpx.HTTPStatusError(
            "503", request=MagicMock(), response=MagicMock(status_code=503)
        )
        client = _client({"items": []}, status_error=error)
        with patch(PATCH, return_value=client), pytest.raises(httpx.HTTPStatusError):
            await get_json("https://example.test/x", {})
        client.get.return_value.json.assert_not_called()

    async def test_a_transport_error_propagates(self):
        client = _client()
        client.get.side_effect = httpx.ConnectError("down")
        with patch(PATCH, return_value=client), pytest.raises(httpx.ConnectError):
            await get_json("https://example.test/x", {})


class TestRecords:
    def test_dict_records_keeps_objects_only(self):
        data = {"docs": [{"key": "a"}, "junk", None, 3, {"key": "b"}]}
        assert dict_records(data, "docs") == [{"key": "a"}, {"key": "b"}]

    def test_dict_records_missing_key_is_empty(self):
        assert dict_records({}, "items") == []

    def test_first_record(self):
        assert first_record({"items": ["x", "y"]}, "items") == "x"

    @pytest.mark.parametrize("data", [{}, {"items": []}])
    def test_first_record_none_when_empty(self, data):
        assert first_record(data, "items") is None


class TestClientsShareTheHelper:
    """Both clients send the request they sent before the helper (#814)."""

    async def test_google_books_isbn_request(self):
        client = _client({"items": [{"id": "v1"}]})
        with (
            patch(PATCH, return_value=client) as cls,
            patch("app.services.google_books.settings.google_books_api_key", ""),
        ):
            assert await GoogleBooksService().search_by_isbn("9780441013593") == {
                "id": "v1"
            }
        cls.assert_called_once_with(timeout=10)
        client.get.assert_awaited_once_with(
            GOOGLE_BOOKS_URL, params={"q": "isbn:9780441013593", "maxResults": 1}
        )

    async def test_google_books_query_request_with_key(self):
        client = _client({"items": [{"id": "1"}, {"id": "2"}, {"id": "3"}]})
        with (
            patch(PATCH, return_value=client),
            patch("app.services.google_books.settings.google_books_api_key", "k"),
        ):
            result = await GoogleBooksService().search_query("dune", limit=2)
        assert result == [{"id": "1"}, {"id": "2"}]
        client.get.assert_awaited_once_with(
            GOOGLE_BOOKS_URL, params={"q": "dune", "maxResults": 2, "key": "k"}
        )

    async def test_open_library_isbn_request(self):
        client = _client({"docs": []})
        service = OpenLibraryService()
        with patch(PATCH, return_value=client) as cls:
            assert await service.search_by_isbn("9780441013593") is None
        cls.assert_called_once_with(timeout=10)
        client.get.assert_awaited_once_with(
            f"{service.base}/search.json",
            params={
                "isbn": "9780441013593",
                "limit": 1,
                "fields": "key,title,subtitle,author_name,isbn,subject",
            },
        )

    async def test_open_library_search_request(self):
        client = _client({"docs": ["junk", {"key": "/works/OL1W"}]})
        service = OpenLibraryService()
        with patch(PATCH, return_value=client):
            result = await service.search("Dune", "Frank Herbert", limit=3)
        assert result == {"key": "/works/OL1W"}
        client.get.assert_awaited_once_with(
            f"{service.base}/search.json",
            params={
                "title": "Dune",
                "limit": 3,
                "fields": "key,title,subtitle,author_name,isbn,subject",
                "author": "Frank Herbert",
            },
        )

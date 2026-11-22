"""Unit tests for GoogleBooksService — all HTTP calls mocked."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.google_books import GoogleBooksService

VOLUME = {
    "id": "gb_dune_001",
    "volumeInfo": {
        "title": "Dune",
        "authors": ["Frank Herbert"],
        "description": "A desert planet epic.",
        "publisher": "Ace Books",
        "publishedDate": "1990-09-01",
        "pageCount": 604,
        "imageLinks": {
            "thumbnail": "http://books.google.com/thumbnail.jpg",
            "large": "http://books.google.com/large.jpg",
        },
    },
}


@pytest.fixture
def service():
    return GoogleBooksService()


def _mock_client(json_data: dict):
    resp = MagicMock()
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()

    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


class TestSearch:
    async def test_returns_first_item_when_found(self, service):
        with patch("app.services._lookup_http.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_client({"items": [VOLUME]})
            result = await service.search("Dune", "Frank Herbert")
        assert result == VOLUME

    async def test_returns_none_when_no_items(self, service):
        with patch("app.services._lookup_http.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_client({})
            result = await service.search("Unknown", "Nobody")
        assert result is None


class TestSearchWithApiKey:
    async def test_includes_key_param_when_api_key_set(self, service):
        with (
            patch("app.services._lookup_http.httpx.AsyncClient") as mock_cls,
            patch("app.services.google_books.settings") as mock_settings,
        ):
            mock_settings.google_books_api_key = "MY_KEY"
            mock_cls.return_value = _mock_client({"items": [VOLUME]})
            await service.search("Dune", "Frank Herbert")

        _, kwargs = mock_cls.return_value.get.call_args
        params = kwargs.get("params") or mock_cls.return_value.get.call_args[0][1]
        assert params.get("key") == "MY_KEY"

    async def test_omits_key_param_when_api_key_not_set(self, service):
        with (
            patch("app.services._lookup_http.httpx.AsyncClient") as mock_cls,
            patch("app.services.google_books.settings") as mock_settings,
        ):
            mock_settings.google_books_api_key = None
            mock_cls.return_value = _mock_client({"items": [VOLUME]})
            await service.search("Dune", "Frank Herbert")

        _, kwargs = mock_cls.return_value.get.call_args
        params = kwargs.get("params") or mock_cls.return_value.get.call_args[0][1]
        assert "key" not in params


class TestSearchQuery:
    async def test_returns_list_of_volumes(self, service):
        with patch("app.services._lookup_http.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_client({"items": [VOLUME, VOLUME]})
            result = await service.search_query("Frank Herbert sci-fi", limit=3)
        assert result == [VOLUME, VOLUME]

    async def test_returns_empty_list_when_no_items(self, service):
        with patch("app.services._lookup_http.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_client({})
            result = await service.search_query("zzz")
        assert result == []

    async def test_respects_limit(self, service):
        volumes = [VOLUME] * 5
        with patch("app.services._lookup_http.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_client({"items": volumes})
            result = await service.search_query("anything", limit=2)
        assert len(result) == 2

    async def test_includes_key_param_when_api_key_set(self, service):
        with (
            patch("app.services._lookup_http.httpx.AsyncClient") as mock_cls,
            patch("app.services.google_books.settings") as mock_settings,
        ):
            mock_settings.google_books_api_key = "MY_KEY"
            mock_cls.return_value = _mock_client({"items": [VOLUME]})
            await service.search_query("Dune", limit=1)

        _, kwargs = mock_cls.return_value.get.call_args
        params = kwargs.get("params") or mock_cls.return_value.get.call_args[0][1]
        assert params.get("key") == "MY_KEY"


class TestSearchByIsbn:
    async def test_returns_first_item_when_found(self, service):
        with patch("app.services._lookup_http.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_client({"items": [VOLUME]})
            result = await service.search_by_isbn("9780441013593")
        assert result == VOLUME

    async def test_returns_none_when_not_found(self, service):
        with patch("app.services._lookup_http.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_client({"items": []})
            result = await service.search_by_isbn("0000000000000")
        assert result is None


class TestExtractCoverUrl:
    def test_prefers_large_image(self, service):
        url = service.extract_cover_url(VOLUME)
        assert "large" in url

    def test_falls_back_to_thumbnail(self, service):
        vol = {
            "volumeInfo": {
                "imageLinks": {"thumbnail": "http://books.google.com/thumb.jpg"}
            }
        }
        url = service.extract_cover_url(vol)
        assert url == "https://books.google.com/thumb.jpg"

    def test_converts_http_to_https(self, service):
        url = service.extract_cover_url(VOLUME)
        assert url.startswith("https://")

    def test_returns_none_when_no_image_links(self, service):
        assert service.extract_cover_url({"volumeInfo": {}}) is None

    def test_returns_none_when_volume_info_missing(self, service):
        assert service.extract_cover_url({}) is None


class TestExtractInfo:
    def test_extracts_all_fields(self, service):
        info = service.extract_info(VOLUME)
        assert info["google_books_id"] == "gb_dune_001"
        assert info["description"] == "A desert planet epic."
        assert info["publisher"] == "Ace Books"
        assert info["publish_year"] == "1990"
        assert info["page_count"] == 604
        assert info["cover_url"].startswith("https://")

    def test_handles_missing_published_date(self, service):
        vol = {"id": "x", "volumeInfo": {}}
        info = service.extract_info(vol)
        assert info["publish_year"] is None

    def test_handles_year_only_published_date(self, service):
        vol = {"id": "x", "volumeInfo": {"publishedDate": "2001"}}
        info = service.extract_info(vol)
        assert info["publish_year"] == "2001"

    def test_handles_completely_empty_volume(self, service):
        info = service.extract_info({})
        assert info["google_books_id"] is None
        assert info["description"] is None
        assert info["cover_url"] is None


# ---------------------------------------------------------------------------
# Error path helpers
# ---------------------------------------------------------------------------


def _mock_error_client(exc: Exception):
    """Create a mock httpx.AsyncClient whose .get() raises the given exception."""
    client = AsyncMock()
    client.get = AsyncMock(side_effect=exc)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _mock_status_error(status_code: int) -> httpx.HTTPStatusError:
    """Build an HTTPStatusError with the given status code."""
    resp = MagicMock()
    resp.status_code = status_code
    return httpx.HTTPStatusError(f"{status_code}", request=MagicMock(), response=resp)


class TestSearchErrorPaths:
    """Verify that HTTP errors propagate from GoogleBooksService.search."""

    async def test_raises_on_http_403_forbidden(self, service):
        with patch("app.services._lookup_http.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_error_client(_mock_status_error(403))
            with pytest.raises(httpx.HTTPStatusError):
                await service.search("Dune", "Frank Herbert")

    async def test_raises_on_http_429_rate_limited(self, service):
        with patch("app.services._lookup_http.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_error_client(_mock_status_error(429))
            with pytest.raises(httpx.HTTPStatusError):
                await service.search("Dune", "Frank Herbert")

    async def test_raises_on_http_500_server_error(self, service):
        with patch("app.services._lookup_http.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_error_client(_mock_status_error(500))
            with pytest.raises(httpx.HTTPStatusError):
                await service.search("Dune", "Frank Herbert")

    async def test_raises_on_timeout(self, service):
        with patch("app.services._lookup_http.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_error_client(
                httpx.TimeoutException("timed out")
            )
            with pytest.raises(httpx.TimeoutException):
                await service.search("Dune", "Frank Herbert")

    async def test_raises_on_connection_error(self, service):
        with patch("app.services._lookup_http.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_error_client(
                httpx.ConnectError("connection refused")
            )
            with pytest.raises(httpx.ConnectError):
                await service.search("Dune", "Frank Herbert")


class TestSearchQueryErrorPaths:
    """Verify that HTTP errors propagate from GoogleBooksService.search_query."""

    async def test_raises_on_http_403_forbidden(self, service):
        with patch("app.services._lookup_http.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_error_client(_mock_status_error(403))
            with pytest.raises(httpx.HTTPStatusError):
                await service.search_query("sci-fi")

    async def test_raises_on_http_429_rate_limited(self, service):
        with patch("app.services._lookup_http.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_error_client(_mock_status_error(429))
            with pytest.raises(httpx.HTTPStatusError):
                await service.search_query("sci-fi")

    async def test_raises_on_timeout(self, service):
        with patch("app.services._lookup_http.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_error_client(
                httpx.TimeoutException("timed out")
            )
            with pytest.raises(httpx.TimeoutException):
                await service.search_query("sci-fi")


class TestSearchByIsbnErrorPaths:
    """Verify that HTTP errors propagate from GoogleBooksService.search_by_isbn."""

    async def test_raises_on_http_500_server_error(self, service):
        with patch("app.services._lookup_http.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_error_client(_mock_status_error(500))
            with pytest.raises(httpx.HTTPStatusError):
                await service.search_by_isbn("9780441013593")

    async def test_raises_on_timeout(self, service):
        with patch("app.services._lookup_http.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_error_client(
                httpx.TimeoutException("timed out")
            )
            with pytest.raises(httpx.TimeoutException):
                await service.search_by_isbn("9780441013593")

    async def test_raises_on_connection_error(self, service):
        with patch("app.services._lookup_http.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_error_client(
                httpx.ConnectError("connection refused")
            )
            with pytest.raises(httpx.ConnectError):
                await service.search_by_isbn("9780441013593")

"""Unit tests for OpenLibraryService — all HTTP calls mocked."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.open_library import OpenLibraryService

WORK_DOC = {
    "key": "/works/OL45804W",
    "title": "Dune",
    "author_name": ["Frank Herbert"],
    "isbn": ["9780441013593"],
    "subject": ["Science fiction", "Desert planets", "Ecology"],
}


@pytest.fixture
def service():
    return OpenLibraryService()


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
    async def test_returns_first_doc_when_found(self, service):
        with patch("app.services._lookup_http.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_client({"docs": [WORK_DOC]})
            result = await service.search("Dune", "Frank Herbert")
        assert result == WORK_DOC

    async def test_returns_none_when_no_docs(self, service):
        with patch("app.services._lookup_http.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_client({"docs": []})
            result = await service.search("Unknown Book", "Unknown Author")
        assert result is None

    async def test_returns_none_when_docs_key_missing(self, service):
        with patch("app.services._lookup_http.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_client({})
            result = await service.search("Dune", "Frank Herbert")
        assert result is None


class TestSearchByIsbn:
    async def test_returns_first_doc_when_found(self, service):
        with patch("app.services._lookup_http.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_client({"docs": [WORK_DOC]})
            result = await service.search_by_isbn("9780441013593")
        assert result == WORK_DOC

    async def test_returns_none_when_not_found(self, service):
        with patch("app.services._lookup_http.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_client({"docs": []})
            result = await service.search_by_isbn("0000000000000")
        assert result is None


class TestExtractWorkId:
    def test_strips_works_prefix(self, service):
        doc = {"key": "/works/OL45804W"}
        assert service.extract_work_id(doc) == "OL45804W"

    def test_returns_none_when_key_missing(self, service):
        assert service.extract_work_id({}) is None

    def test_returns_none_when_key_empty(self, service):
        assert service.extract_work_id({"key": ""}) is None

    def test_handles_bare_id(self, service):
        doc = {"key": "OL45804W"}
        assert service.extract_work_id(doc) == "OL45804W"


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
    resp = MagicMock()
    resp.status_code = status_code
    return httpx.HTTPStatusError(f"{status_code}", request=MagicMock(), response=resp)


class TestSearchErrorPaths:
    """Verify that HTTP errors propagate from OpenLibraryService.search."""

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


class TestSearchByIsbnErrorPaths:
    """Verify that HTTP errors propagate from OpenLibraryService.search_by_isbn."""

    async def test_raises_on_http_500_server_error(self, service):
        with patch("app.services._lookup_http.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_error_client(_mock_status_error(500))
            with pytest.raises(httpx.HTTPStatusError):
                await service.search_by_isbn("9780441013593")

    async def test_raises_on_http_429_rate_limited(self, service):
        with patch("app.services._lookup_http.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_error_client(_mock_status_error(429))
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

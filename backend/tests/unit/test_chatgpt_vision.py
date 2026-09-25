"""Unit tests for ChatGPTVisionIdentifier — all HTTP calls mocked."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.book_identifier import ScanUnavailableError
from app.services.chatgpt_vision import ChatGPTVisionIdentifier

IMAGE_BYTES = b"fakeimagebytes"

VALID_RESPONSE = json.dumps(
    [
        {
            "title": "Dune",
            "author": "Frank Herbert",
            "confidence": 0.97,
            "isbn_13": "9780441013593",
            "isbn_10": None,
        },
        {
            "title": "Dune Messiah",
            "author": "Frank Herbert",
            "confidence": 0.6,
            "isbn_13": None,
            "isbn_10": None,
        },
    ]
)


def _make_mock_response(content: str, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {"choices": [{"message": {"content": content}}]}
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        from httpx import HTTPStatusError

        resp.raise_for_status.side_effect = HTTPStatusError(
            "error", request=MagicMock(), response=MagicMock()
        )
    return resp


@pytest.fixture
def identifier():
    return ChatGPTVisionIdentifier()


class TestIdentifySuccess:
    async def test_returns_candidates_from_valid_json(self, identifier):
        mock_resp = _make_mock_response(VALID_RESPONSE)
        with patch("app.services.chatgpt_vision.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            candidates = await identifier.identify(IMAGE_BYTES)

        assert len(candidates) == 2
        assert candidates[0].title == "Dune"
        assert candidates[0].author == "Frank Herbert"
        assert candidates[0].confidence == 0.97
        assert candidates[0].isbn_13 == "9780441013593"
        assert candidates[0].isbn_10 is None

    async def test_caps_at_max_books(self, identifier):
        many_books = json.dumps(
            {
                "books": [
                    {
                        "title": f"Book {i}",
                        "author": "Author",
                        "confidence": 0.9,
                        "isbn_13": None,
                        "isbn_10": None,
                    }
                    for i in range(20)
                ]
            }
        )
        mock_resp = _make_mock_response(many_books)
        with patch("app.services.chatgpt_vision.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            candidates = await identifier.identify(IMAGE_BYTES)

        assert len(candidates) == 15

    async def test_skips_malformed_items(self, identifier):
        bad_json = json.dumps(
            [
                {
                    "title": "Good Book",
                    "author": "Author",
                    "confidence": 0.9,
                    "isbn_13": None,
                    "isbn_10": None,
                },
                {"title_typo": "Missing author field"},  # malformed
            ]
        )
        mock_resp = _make_mock_response(bad_json)
        with patch("app.services.chatgpt_vision.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            candidates = await identifier.identify(IMAGE_BYTES)

        assert len(candidates) == 1
        assert candidates[0].title == "Good Book"

    async def test_defaults_confidence_to_0_5_when_missing(self, identifier):
        no_confidence = json.dumps(
            [{"title": "A Book", "author": "Author", "isbn_13": None, "isbn_10": None}]
        )
        mock_resp = _make_mock_response(no_confidence)
        with patch("app.services.chatgpt_vision.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            candidates = await identifier.identify(IMAGE_BYTES)

        assert candidates[0].confidence == 0.5


class TestIdentifyFailures:
    async def test_returns_empty_on_non_json_response(self, identifier):
        mock_resp = _make_mock_response("Sorry, I cannot identify this image.")
        with patch("app.services.chatgpt_vision.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            candidates = await identifier.identify(IMAGE_BYTES)

        assert candidates == []

    async def test_raises_scan_unavailable_on_http_error(self, identifier):
        with patch("app.services.chatgpt_vision.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(
                side_effect=httpx.HTTPStatusError(
                    "401", request=MagicMock(), response=MagicMock()
                )
            )
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(ScanUnavailableError):
                await identifier.identify(IMAGE_BYTES)

    async def test_raises_scan_unavailable_on_timeout(self, identifier):
        with patch("app.services.chatgpt_vision.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(
                side_effect=httpx.TimeoutException("timed out")
            )
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(ScanUnavailableError):
                await identifier.identify(IMAGE_BYTES)

    async def test_raises_scan_unavailable_on_403_forbidden(self, identifier):
        with patch("app.services.chatgpt_vision.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(
                side_effect=httpx.HTTPStatusError(
                    "403", request=MagicMock(), response=MagicMock()
                )
            )
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(ScanUnavailableError):
                await identifier.identify(IMAGE_BYTES)

    async def test_raises_scan_unavailable_on_429_rate_limited(self, identifier):
        with patch("app.services.chatgpt_vision.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(
                side_effect=httpx.HTTPStatusError(
                    "429", request=MagicMock(), response=MagicMock()
                )
            )
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(ScanUnavailableError):
                await identifier.identify(IMAGE_BYTES)

    async def test_raises_scan_unavailable_on_500_server_error(self, identifier):
        with patch("app.services.chatgpt_vision.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(
                side_effect=httpx.HTTPStatusError(
                    "500", request=MagicMock(), response=MagicMock()
                )
            )
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(ScanUnavailableError):
                await identifier.identify(IMAGE_BYTES)

    async def test_propagates_connection_error(self, identifier):
        """ConnectError is not caught — it propagates to the caller."""
        with patch("app.services.chatgpt_vision.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(
                side_effect=httpx.ConnectError("connection refused")
            )
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(httpx.ConnectError):
                await identifier.identify(IMAGE_BYTES)


def _patched_client(mock_resp):
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


class TestMultiBook:
    async def test_parses_books_object_shape(self, identifier):
        body = json.dumps(
            {
                "books": [
                    {"title": "Dune", "author": "Frank Herbert", "confidence": 0.9},
                    {"title": "Emma", "author": "Jane Austen", "confidence": 0.8},
                ]
            }
        )
        with patch("app.services.chatgpt_vision.httpx.AsyncClient") as cls:
            cls.return_value = _patched_client(_make_mock_response(body))
            candidates = await identifier.identify(IMAGE_BYTES)

        assert [c.title for c in candidates] == ["Dune", "Emma"]

    async def test_drops_duplicate_books(self, identifier):
        body = json.dumps(
            {
                "books": [
                    {"title": "Dune", "author": "Frank Herbert"},
                    {"title": " dune ", "author": "FRANK HERBERT"},
                ]
            }
        )
        with patch("app.services.chatgpt_vision.httpx.AsyncClient") as cls:
            cls.return_value = _patched_client(_make_mock_response(body))
            candidates = await identifier.identify(IMAGE_BYTES)

        assert len(candidates) == 1

    async def test_returns_empty_for_unexpected_shape(self, identifier):
        body = json.dumps({"books": "none"})
        with patch("app.services.chatgpt_vision.httpx.AsyncClient") as cls:
            cls.return_value = _patched_client(_make_mock_response(body))
            candidates = await identifier.identify(IMAGE_BYTES)

        assert candidates == []

    async def test_skips_non_object_items(self, identifier):
        body = json.dumps({"books": ["Dune", {"title": "Emma", "author": "Austen"}]})
        with patch("app.services.chatgpt_vision.httpx.AsyncClient") as cls:
            cls.return_value = _patched_client(_make_mock_response(body))
            candidates = await identifier.identify(IMAGE_BYTES)

        assert [c.title for c in candidates] == ["Emma"]

    async def test_standard_scan_uses_standard_model_in_json_mode(self):
        with patch("app.services.chatgpt_vision.httpx.AsyncClient") as cls:
            client = _patched_client(_make_mock_response('{"books": []}'))
            cls.return_value = client
            await ChatGPTVisionIdentifier().identify(IMAGE_BYTES)

        payload = client.post.call_args.kwargs["json"]
        assert payload["model"] == "gpt-4o-mini"
        assert payload["response_format"] == {"type": "json_object"}
        image_part = payload["messages"][1]["content"][0]
        assert image_part["image_url"]["detail"] == "high"

    async def test_enhanced_scan_uses_enhanced_model(self):
        with patch("app.services.chatgpt_vision.httpx.AsyncClient") as cls:
            client = _patched_client(_make_mock_response('{"books": []}'))
            cls.return_value = client
            await ChatGPTVisionIdentifier(enhanced=True).identify(IMAGE_BYTES)

        assert client.post.call_args.kwargs["json"]["model"] == "gpt-4o"

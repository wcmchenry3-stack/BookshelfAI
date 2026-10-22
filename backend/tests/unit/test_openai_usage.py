"""OpenAI provider: billed usage and outcome mapping (#530). HTTP mocked."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.ai.providers.base import ProviderResult, ProviderTimeoutError
from app.ai.providers.openai_vision import OpenAIVisionProvider
from app.ai.registry import FEATURES
from app.services.book_identifier import BookCandidate, ScanUnavailableError

BOOKS = json.dumps({"books": [{"title": "Dune", "author": "Frank Herbert"}]})
DUNE = BookCandidate(title="Dune", author="Frank Herbert", confidence=0.5)


def _client(body=None, error=None):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = body
    client = MagicMock()
    client.post = AsyncMock(return_value=resp, side_effect=error)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _body(content=BOOKS, finish_reason="stop", usage=None, **message):
    body = {
        "choices": [
            {
                "message": {"content": content, **message},
                "finish_reason": finish_reason,
            }
        ]
    }
    if usage is not None:
        body["usage"] = usage
    return body


async def _identify(body=None, error=None) -> ProviderResult:
    with patch(
        "app.ai.providers.openai_vision.httpx.AsyncClient",
        return_value=_client(body, error),
    ):
        return await OpenAIVisionProvider(FEATURES["scan.standard"]).identify(b"img")


class TestUsage:
    async def test_tokens_mapped_and_cached_split_out(self):
        result = await _identify(
            _body(
                usage={
                    "prompt_tokens": 1_200,
                    "completion_tokens": 80,
                    "total_tokens": 1_280,
                    "prompt_tokens_details": {"cached_tokens": 1_024},
                }
            )
        )
        assert result == ProviderResult(
            candidates=[DUNE],
            input_tokens=176,  # 1,200 prompt tokens minus 1,024 cached
            output_tokens=80,
            cache_read_tokens=1_024,
            cache_write_tokens=0,
            outcome="ok",
        )

    async def test_no_cached_details_means_zero_cache_read(self):
        result = await _identify(
            _body(usage={"prompt_tokens": 900, "completion_tokens": 40})
        )
        assert (result.input_tokens, result.output_tokens) == (900, 40)
        assert (result.cache_read_tokens, result.cache_write_tokens) == (0, 0)

    @pytest.mark.parametrize(
        "usage",
        [
            None,
            "garbled",
            [],
            {},
            {"prompt_tokens": "12", "completion_tokens": None},
            {"prompt_tokens": -5, "completion_tokens": True},
            {"prompt_tokens": 1.5, "prompt_tokens_details": "x"},
            {"prompt_tokens_details": {"cached_tokens": 50}},
        ],
    )
    async def test_missing_or_garbled_usage_gives_zeros(self, usage):
        body = _body()
        if usage is not None:
            body["usage"] = usage
        result = await _identify(body)
        assert result.candidates == [DUNE]
        assert (
            result.input_tokens,
            result.output_tokens,
            result.cache_read_tokens,
            result.cache_write_tokens,
        ) == (0, 0, 0, 0)

    @pytest.mark.parametrize(
        ("reported", "stored"),
        [
            (2_147_483_647, 2_147_483_647),
            (2_147_483_648, 2_147_483_647),
            (10**30, 2_147_483_647),
            (-1, 0),
        ],
    )
    async def test_counts_clamped_to_the_integer_column(self, reported, stored):
        result = await _identify(
            _body(usage={"prompt_tokens": reported, "completion_tokens": reported})
        )
        assert (result.input_tokens, result.output_tokens) == (stored, stored)

    async def test_usage_kept_when_body_is_not_a_dict(self):
        result = await _identify(None)
        assert result.candidates == []
        assert result.input_tokens == 0


class TestOutcome:
    async def test_normal_stop_is_ok(self):
        assert (await _identify(_body())).outcome == "ok"

    async def test_length_finish_is_truncated(self):
        result = await _identify(
            _body(
                content='{"books": [{"title": "Du',
                finish_reason="length",
                usage={"prompt_tokens": 10, "completion_tokens": 4096},
            )
        )
        assert result.outcome == "truncated"
        assert result.candidates == []
        assert result.output_tokens == 4096

    async def test_refusal_field_is_refused(self):
        result = await _identify(_body(content=None, refusal="I can't help with that."))
        assert result.outcome == "refused"
        assert result.candidates == []

    async def test_refusal_alongside_content_is_refused(self):
        result = await _identify(_body(content='{"books": []}', refusal="No."))
        assert result.outcome == "refused"

    async def test_content_filter_is_refused(self):
        result = await _identify(_body(content=None, finish_reason="content_filter"))
        assert result.outcome == "refused"

    @pytest.mark.parametrize(
        "body",
        [
            {"choices": []},
            {"choices": [{}]},
            {"error": {}},
            [],
            None,
            {"choices": [{"message": {"content": None}, "finish_reason": "stop"}]},
            {"choices": [{"message": {"content": ["not", "text"]}}]},
            {"choices": [{"message": {"content": "not json"}}]},
            {"choices": [{"message": {"content": '{"books": "none"}'}}]},
            {"choices": [{"message": {"content": None, "refusal": ""}}]},
        ],
    )
    async def test_no_or_unusable_content_without_refusal_is_ok(self, body):
        """Only a refusal field or a content filter is a refusal; a 200 with
        no usable content is an answered call with no books."""
        result = await _identify(body)
        assert result.outcome == "ok"
        assert result.candidates == []

    async def test_no_content_keeps_the_real_usage(self):
        body = {
            "choices": [{"message": {"content": None}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1_500, "completion_tokens": 3},
        }
        result = await _identify(body)
        assert (result.outcome, result.input_tokens, result.output_tokens) == (
            "ok",
            1_500,
            3,
        )

    async def test_parsing_unchanged_for_valid_reply(self):
        result = await _identify(_body(usage={"prompt_tokens": 1}))
        assert result.candidates == [DUNE]


class TestErrors:
    async def test_http_timeout_raises_provider_timeout(self):
        with pytest.raises(ProviderTimeoutError) as exc_info:
            await _identify(error=httpx.ReadTimeout("slow"))
        # Still a ScanUnavailableError, so /scan refunds it.
        assert isinstance(exc_info.value, ScanUnavailableError)

    async def test_http_error_is_not_a_timeout(self):
        with pytest.raises(ScanUnavailableError) as exc_info:
            await _identify(
                error=httpx.HTTPStatusError(
                    "500", request=MagicMock(), response=MagicMock(status_code=500)
                )
            )
        assert not isinstance(exc_info.value, ProviderTimeoutError)


def test_parse_uses_the_max_books_it_is_given():
    """identify reads settings.scan_max_books once and passes it on."""
    content = json.dumps(
        {"books": [{"title": f"Book {i}", "author": "A"} for i in range(5)]}
    )
    provider = OpenAIVisionProvider(FEATURES["scan.standard"])
    assert len(provider._parse(content, "stop", 2)) == 2


async def test_identify_reads_max_books_once():
    reads = []

    class CountingSettings:
        openai_api_key = "k"

        @property
        def scan_max_books(self):
            reads.append(1)
            return 1

    body = json.dumps(
        {"books": [{"title": "A", "author": "X"}, {"title": "B", "author": "Y"}]}
    )
    with (
        patch("app.ai.providers.openai_vision.settings", CountingSettings()),
        patch(
            "app.ai.providers.openai_vision.httpx.AsyncClient",
            return_value=_client(_body(content=body)),
        ),
    ):
        result = await OpenAIVisionProvider(FEATURES["scan.standard"]).identify(b"i")
    assert len(result.candidates) == 1
    assert reads == [1]

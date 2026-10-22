"""Malformed provider replies (#572): both adapters report ``malformed`` for a
200 whose reply is missing, not JSON, of the wrong shape, a non-empty list in
which no item validates, or that ended on an unexpected stop reason. A
genuine ``{"books": []}`` stays ``ok`` with no candidates. SDK / HTTP mocked;
never a real model."""

import dataclasses
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai.providers.anthropic_vision import AnthropicVisionProvider
from app.ai.providers.openai_vision import OpenAIVisionProvider
from app.ai.registry import FEATURES, default_model
from tests._claude_fake import fake_claude, message

IMAGE = b"\xff\xd8\xff-fake-jpeg"
SECRET = "Zxq Secret Shelf Title"
DUNE = {"title": "Dune", "author": "Frank Herbert", "confidence": 0.9}
BAD_ITEMS = [
    {"title": "", "author": "A"},
    {"title": "T"},
    {"author": "A"},
    {"title": None, "author": "A"},
    {"title": 5, "author": "A"},
    "Dune",
    None,
]

# Shapes that are malformed for both adapters (the text of an end_turn / stop
# reply).
MALFORMED_TEXT = {
    "not_json": f"Sorry, I cannot read {SECRET}.",
    "cut_json": f'{{"books": [{{"title": "{SECRET}", "auth',
    "json_null": "null",
    "json_string": '"no books"',
    "json_number": "42",
    "object_without_books": '{"titles": []}',
    "empty_object": "{}",
    "books_not_a_list": '{"books": "none"}',
    "books_null": '{"books": null}',
    "books_an_object": '{"books": {"title": "Dune"}}',
    "every_item_invalid": json.dumps({"books": BAD_ITEMS}),
    "one_invalid_item": json.dumps({"books": [{"title": SECRET}]}),
}


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------


@pytest.fixture
def claude():
    spec = dataclasses.replace(
        FEATURES["scan.standard"],
        provider="anthropic",
        model=default_model("scan.standard", "anthropic"),
    )
    return AnthropicVisionProvider(spec)


async def _claude(provider, reply):
    with fake_claude(reply):
        return await provider.identify(IMAGE)


class TestClaude:
    @pytest.mark.parametrize("text", MALFORMED_TEXT.values(), ids=MALFORMED_TEXT)
    async def test_malformed_content(self, claude, text, caplog):
        with caplog.at_level(logging.DEBUG):
            result = await _claude(claude, message(text))
        assert result.outcome == "malformed"
        assert result.candidates == []
        # The billed usage is kept for the ledger and the breaker.
        assert (result.input_tokens, result.output_tokens) == (1_200, 300)
        assert SECRET not in caplog.text and "Zxq" not in caplog.text

    async def test_bare_list_is_malformed(self, claude):
        """Claude's schema is an object; a bare list is the wrong shape."""
        result = await _claude(claude, message(json.dumps([DUNE])))
        assert result.outcome == "malformed"

    @pytest.mark.parametrize("thinking", [False, True])
    async def test_missing_text_is_malformed(self, claude, thinking):
        result = await _claude(claude, message("", thinking=thinking))
        assert result.outcome == "malformed"
        assert result.candidates == []

    @pytest.mark.parametrize(
        "stop_reason",
        [
            "tool_use",
            "pause_turn",
            "stop_sequence",
            "model_context_window_exceeded",
            None,
        ],
    )
    async def test_unexpected_stop_reason_is_malformed(
        self, claude, stop_reason, caplog
    ):
        reply = message(json.dumps({"books": [{**DUNE, "title": SECRET}]}))
        reply.stop_reason = stop_reason
        with caplog.at_level(logging.DEBUG):
            result = await _claude(claude, reply)
        assert result.outcome == "malformed"
        assert result.candidates == []
        assert result.input_tokens == 1_200
        assert SECRET not in caplog.text
        assert "stop_reason=" in caplog.text

    async def test_genuine_empty_list_is_ok(self, claude):
        result = await _claude(claude, message(books=[]))
        assert (result.outcome, result.candidates) == ("ok", [])

    async def test_some_items_valid_is_ok_with_the_valid_ones(self, claude):
        result = await _claude(
            claude, message(json.dumps({"books": [*BAD_ITEMS, DUNE]}))
        )
        assert result.outcome == "ok"
        assert [c.title for c in result.candidates] == ["Dune"]

    async def test_refusal_unchanged(self, claude):
        result = await _claude(claude, message("not json", stop_reason="refusal"))
        assert (result.outcome, result.candidates) == ("refused", [])

    @pytest.mark.parametrize("text", ['{"books": "x"}', "not json", '{"boo'])
    async def test_truncated_unchanged(self, claude, text):
        result = await _claude(claude, message(text, stop_reason="max_tokens"))
        assert (result.outcome, result.candidates) == ("truncated", [])


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------


def _openai_body(content, finish_reason="stop", **message_fields):
    return {
        "choices": [
            {
                "message": {"content": content, **message_fields},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 1_000, "completion_tokens": 40},
    }


async def _openai(body):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = body
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    with patch("app.ai.providers.openai_vision.httpx.AsyncClient", return_value=client):
        return await OpenAIVisionProvider(FEATURES["scan.standard"]).identify(IMAGE)


class TestOpenAI:
    @pytest.mark.parametrize("text", MALFORMED_TEXT.values(), ids=MALFORMED_TEXT)
    async def test_malformed_content(self, text, caplog):
        with caplog.at_level(logging.DEBUG):
            result = await _openai(_openai_body(text))
        assert result.outcome == "malformed"
        assert result.candidates == []
        assert (result.input_tokens, result.output_tokens) == (1_000, 40)
        assert SECRET not in caplog.text and "Zxq" not in caplog.text

    async def test_every_item_invalid_in_a_bare_list_is_malformed(self):
        result = await _openai(_openai_body(json.dumps(BAD_ITEMS)))
        assert result.outcome == "malformed"

    @pytest.mark.parametrize(
        "body",
        [
            _openai_body(None),
            _openai_body(["not", "text"]),
            {"choices": []},
            {"usage": {"prompt_tokens": 5}},
            None,
        ],
        ids=[
            "content_null",
            "content_not_text",
            "no_choices",
            "no_choices_key",
            "no_body",
        ],
    )
    async def test_missing_content_is_malformed(self, body):
        result = await _openai(body)
        assert (result.outcome, result.candidates) == ("malformed", [])

    @pytest.mark.parametrize("finish_reason", ["tool_calls", "function_call", "x"])
    async def test_unexpected_finish_reason_is_malformed(self, finish_reason, caplog):
        body = _openai_body(
            json.dumps({"books": [{**DUNE, "title": SECRET}]}), finish_reason
        )
        with caplog.at_level(logging.DEBUG):
            result = await _openai(body)
        assert (result.outcome, result.candidates) == ("malformed", [])
        assert result.input_tokens == 1_000
        assert SECRET not in caplog.text

    @pytest.mark.parametrize("content", ['{"books": []}', "[]", ' {"books": [] } '])
    async def test_genuine_empty_list_is_ok(self, content):
        result = await _openai(_openai_body(content))
        assert (result.outcome, result.candidates) == ("ok", [])

    async def test_missing_finish_reason_is_judged_on_the_content(self):
        body = _openai_body(json.dumps({"books": [DUNE]}), finish_reason=None)
        result = await _openai(body)
        assert result.outcome == "ok"
        assert [c.title for c in result.candidates] == ["Dune"]

    async def test_some_items_valid_is_ok_with_the_valid_ones(self):
        result = await _openai(_openai_body(json.dumps({"books": [*BAD_ITEMS, DUNE]})))
        assert result.outcome == "ok"
        assert [c.title for c in result.candidates] == ["Dune"]

    async def test_refusal_unchanged(self):
        result = await _openai(_openai_body(None, refusal="I can't help with that."))
        assert (result.outcome, result.candidates) == ("refused", [])

    async def test_truncated_unchanged(self):
        result = await _openai(_openai_body('{"books": [{"title": "Du', "length"))
        assert (result.outcome, result.candidates) == ("truncated", [])

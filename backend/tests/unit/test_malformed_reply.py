"""Malformed provider replies (#572): both adapters report ``malformed`` for a
200 whose reply is missing, not JSON, of the wrong shape, a non-empty list in
which every item is structurally broken, or that ended on an unexpected stop
reason. Schema-valid items keep each adapter's handling from before #572
(Claude drops an empty or whitespace title or author; OpenAI keeps them), so
such a reply is ``ok``. A genuine ``{"books": []}`` stays ``ok`` with no
candidates. A reply cut off by the token limit with no book is reported as
``malformed`` by the gateway. SDK / HTTP mocked; never a real model."""

import dataclasses
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai.gateway import VisionPayload, reported_outcome, run_feature
from app.ai.providers.anthropic_vision import (
    MAX_TEXT_LENGTH,
    AnthropicVisionProvider,
    parse_reply,
)
from app.ai.providers.base import ProviderResult
from app.ai.providers.openai_vision import OpenAIVisionProvider
from app.ai.registry import FEATURES, default_model
from app.services.book_identifier import BookCandidate
from tests._claude_fake import fake_claude, message

IMAGE = b"\xff\xd8\xff-fake-jpeg"
SECRET = "Zxq Secret Shelf Title"
DUNE = {"title": "Dune", "author": "Frank Herbert", "confidence": 0.9}
# Items that break the requested structure for both adapters: not an object,
# or a title or author missing or not a string.
BAD_ITEMS = [
    {"title": "T"},
    {"author": "A"},
    {"title": None, "author": "A"},
    {"title": 5, "author": "A"},
    {"title": "T", "author": None},
    "Dune",
    None,
    [],
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
    async def test_truncated_with_no_books_is_truncated_here(self, claude, text):
        """The adapter reports the stop reason; the gateway turns a truncated
        reply with no books into ``malformed`` (see TestTruncatedNoBooks)."""
        result = await _claude(claude, message(text, stop_reason="max_tokens"))
        assert (result.outcome, result.candidates) == ("truncated", [])

    @pytest.mark.parametrize(
        "item",
        [
            {"title": "Dune", "author": ""},
            {"title": "Dune", "author": "   "},
            {"title": "", "author": "Frank Herbert"},
            {"title": " \t ", "author": "Frank Herbert"},
        ],
        ids=["empty_author", "blank_author", "empty_title", "blank_title"],
    )
    async def test_empty_or_blank_text_is_dropped_not_malformed(self, claude, item):
        """Schema-valid (strings) but empty: dropped as before #572, so the
        reply is an answer with no books, not a malformed one."""
        result = await _claude(claude, message(json.dumps({"books": [item]})))
        assert (result.outcome, result.candidates) == ("ok", [])

    async def test_a_title_over_the_maximum_is_broken(self, claude):
        long_title = {"title": "x" * (MAX_TEXT_LENGTH + 1), "author": "A"}
        result = await _claude(claude, message(json.dumps({"books": [long_title]})))
        assert (result.outcome, result.candidates) == ("malformed", [])


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

    async def test_truncated_with_no_books_is_truncated_here(self):
        """The gateway turns it into ``malformed`` (see TestTruncatedNoBooks)."""
        result = await _openai(_openai_body('{"books": [{"title": "Du', "length"))
        assert (result.outcome, result.candidates) == ("truncated", [])

    async def test_empty_author_is_still_a_candidate(self):
        """As before #572: a title-only lookup, not a malformed reply."""
        body = _openai_body(json.dumps({"books": [{"title": "Dune", "author": ""}]}))
        result = await _openai(body)
        assert result.outcome == "ok"
        assert [(c.title, c.author) for c in result.candidates] == [("Dune", "")]

    async def test_a_confidence_that_is_not_a_number_is_broken(self):
        item = {"title": "Dune", "author": "A", "confidence": "high"}
        result = await _openai(_openai_body(json.dumps({"books": [item]})))
        assert (result.outcome, result.candidates) == ("malformed", [])


# ---------------------------------------------------------------------------
# Old vs new parser: the candidates are unchanged (#572 review)
# ---------------------------------------------------------------------------

_LONG = "x" * 501
_MIXED = [
    {"title": "Dune", "author": ""},
    {"title": "   ", "author": "Frank Herbert"},
    {"title": "Emma", "author": None},
    "Dune",
    DUNE,
]

# fixture -> (items, Claude's candidates, OpenAI's candidates), as
# (title, author) pairs; None is malformed (no candidates, a 503). Every
# non-None value is what the parser on ``dev`` before #572 returned; where
# the old parser returned [] for a reply that is now malformed, the
# candidates are the same (none). The old OpenAI parser crashed on a null
# author (in the de-duplication), now that item is broken and dropped. Since
# #746 OpenAI also drops an empty or whitespace title (it kept it before).
COMPARISON = {
    "empty_author": ([{"title": "Dune", "author": ""}], [], [("Dune", "")]),
    "whitespace_title": (
        [{"title": "   ", "author": "Frank Herbert"}],
        [],
        [],  # #746: an empty or whitespace title is dropped
    ),
    "title_over_500": ([{"title": _LONG, "author": "A"}], None, [(_LONG, "A")]),
    "null_author": ([{"title": "Dune", "author": None}], None, None),
    "mixed": (
        _MIXED,
        [("Dune", "Frank Herbert")],
        [("Dune", ""), ("Dune", "Frank Herbert")],  # #746: blank title dropped
    ),
}


def _pairs(candidates: list[BookCandidate] | None):
    if candidates is None:
        return None
    return [(c.title, c.author) for c in candidates]


@pytest.mark.parametrize(
    ("items", "claude_expected", "openai_expected"),
    COMPARISON.values(),
    ids=COMPARISON,
)
class TestOldVsNewParser:
    def test_claude(self, items, claude_expected, openai_expected):
        text = json.dumps({"books": items})
        assert _pairs(parse_reply(text, "end_turn", 20)) == claude_expected

    def test_openai(self, items, claude_expected, openai_expected):
        provider = OpenAIVisionProvider(FEATURES["scan.standard"])
        text = json.dumps({"books": items})
        assert _pairs(provider._parse_reply(text, "stop", 20)) == openai_expected

    async def test_claude_outcome(
        self, claude, items, claude_expected, openai_expected
    ):
        result = await _claude(claude, message(json.dumps({"books": items})))
        assert result.outcome == ("malformed" if claude_expected is None else "ok")

    async def test_openai_outcome(self, items, claude_expected, openai_expected):
        result = await _openai(_openai_body(json.dumps({"books": items})))
        assert result.outcome == ("malformed" if openai_expected is None else "ok")


# ---------------------------------------------------------------------------
# A truncated reply with no books is malformed (owner decision on #572)
# ---------------------------------------------------------------------------

_DUNE_CANDIDATE = BookCandidate(title="Dune", author="Frank Herbert", confidence=0.9)
_COMPLETE_DUNE = (
    '{"title": "Dune", "author": "Frank Herbert", "confidence": 0.9, '
    '"isbn_13": null, "isbn_10": null}'
)


class TestTruncatedNoBooks:
    @pytest.mark.parametrize(
        ("outcome", "candidates", "reported"),
        [
            ("truncated", [], "malformed"),
            ("truncated", [_DUNE_CANDIDATE], "truncated"),
            ("ok", [], "ok"),
            ("refused", [], "refused"),
            ("malformed", [], "malformed"),
        ],
    )
    def test_reported_outcome(self, outcome, candidates, reported):
        assert reported_outcome(ProviderResult(candidates, outcome=outcome)) == (
            reported
        )

    async def _run(self, feature, user_spec_provider, reply_patch):
        tier = patch(
            "app.ai.gateway.entitlements.tier_for", AsyncMock(return_value="free")
        )
        with tier, reply_patch:
            return await run_feature(
                feature,
                user=MagicMock(),
                db=MagicMock(),
                payload=VisionPayload(image_bytes=IMAGE),
                settings_snapshot=user_spec_provider,
            )

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ('{"books": [{"title": "Du', ("malformed", [])),
            ("not json", ("malformed", [])),
            (f'{{"books": [{_COMPLETE_DUNE}, {{"title": "Em', ("truncated", ["Dune"])),
        ],
        ids=["cut_in_first_book", "no_json", "one_complete_book"],
    )
    async def test_claude_through_the_gateway(self, text, expected):
        from app.ai import settings as ai_settings

        snapshot = ai_settings.AISettings(
            {
                "scan.standard.provider": "anthropic",
                "scan.standard.model": default_model("scan.standard", "anthropic"),
            }
        )
        result = await self._run(
            "scan.standard",
            snapshot,
            fake_claude(message(text, stop_reason="max_tokens")),
        )
        assert result.provider == "anthropic"
        assert (result.outcome, [c.title for c in result.candidates]) == expected
        assert result.input_tokens == 1_200  # billed, kept for the ledger

    async def test_openai_through_the_gateway(self):
        from app.ai import settings as ai_settings

        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = _openai_body('{"books": [{"title": "Du', "length")
        client = MagicMock()
        client.post = AsyncMock(return_value=resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        result = await self._run(
            "scan.standard",
            ai_settings.AISettings({}),
            patch(
                "app.ai.providers.openai_vision.httpx.AsyncClient", return_value=client
            ),
        )
        assert result.provider == "openai"
        assert (result.outcome, result.candidates) == ("malformed", [])
        assert result.input_tokens == 1_000

"""Malformed provider replies (#572): the adapter reports ``malformed`` for a
200 whose reply is missing, not JSON, of the wrong shape, a non-empty list in
which every item is structurally broken, or that ended on an unexpected stop
reason. Schema-valid items keep the handling from before #572 (Claude drops
an empty or whitespace title or author), so such a reply is ``ok``. A genuine ``{"books": []}`` stays ``ok`` with no
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
from app.ai.registry import FEATURES, default_model
from app.services.book_identifier import BookCandidate
from tests._claude_fake import fake_claude, message

IMAGE = b"\xff\xd8\xff-fake-jpeg"
SECRET = "Zxq Secret Shelf Title"
DUNE = {"title": "Dune", "author": "Frank Herbert", "confidence": 0.9}
# Items that break the requested structure: not an object,
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

# Shapes that are malformed (the text of an end_turn reply).
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

# fixture -> (items, the candidates), as (title, author) pairs; None is
# malformed (no candidates, a 503). Every non-None value is what the parser on
# ``dev`` before #572 returned; where the old parser returned [] for a reply
# that is now malformed, the candidates are the same (none).
COMPARISON = {
    "empty_author": ([{"title": "Dune", "author": ""}], []),
    "whitespace_title": ([{"title": "   ", "author": "Frank Herbert"}], []),
    "title_over_500": ([{"title": _LONG, "author": "A"}], None),
    "null_author": ([{"title": "Dune", "author": None}], None),
    "mixed": (_MIXED, [("Dune", "Frank Herbert")]),
}


def _pairs(candidates: list[BookCandidate] | None):
    if candidates is None:
        return None
    return [(c.title, c.author) for c in candidates]


@pytest.mark.parametrize(
    ("items", "expected"),
    COMPARISON.values(),
    ids=COMPARISON,
)
class TestOldVsNewParser:
    def test_candidates(self, items, expected):
        text = json.dumps({"books": items})
        assert _pairs(parse_reply(text, "end_turn", 20)) == expected

    async def test_outcome(self, claude, items, expected):
        result = await _claude(claude, message(json.dumps({"books": items})))
        assert result.outcome == ("malformed" if expected is None else "ok")


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

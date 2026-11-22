"""The reply-parsing helpers shared by the vision adapters (#814,
``app/ai/providers/_common.py``)."""

import json
import math

import pytest

from app.ai.providers._common import (
    MAX_TEXT_LENGTH,
    MAX_TOKEN_COUNT,
    clamp_confidence,
    collect_candidates,
    has_broken_text,
    token_count,
)
from app.ai.providers.anthropic_vision import parse_reply
from app.ai.providers.openai_vision import OpenAIVisionProvider
from app.ai.registry import FEATURES
from app.services.book_identifier import BookCandidate


class TestHasBrokenText:
    @pytest.mark.parametrize(
        "item",
        [
            None,
            "Dune",
            ["Dune", "Herbert"],
            {"author": "Herbert"},
            {"title": "Dune"},
            {"title": None, "author": "Herbert"},
            {"title": "Dune", "author": 7},
            {"title": "x" * (MAX_TEXT_LENGTH + 1), "author": "A"},
            {"title": "T", "author": "x" * (MAX_TEXT_LENGTH + 1)},
        ],
    )
    def test_broken(self, item):
        assert has_broken_text(item) is True

    @pytest.mark.parametrize(
        "item",
        [
            {"title": "Dune", "author": "Herbert"},
            # Empty or blank is schema-valid: dropped later, not broken.
            {"title": "", "author": ""},
            {"title": "   ", "author": "Herbert"},
            # The limit applies after stripping.
            {"title": "  " + "x" * MAX_TEXT_LENGTH + "  ", "author": "A"},
            # Other keys, a bad confidence included, are not looked at here.
            {"title": "T", "author": "A", "confidence": "high"},
        ],
    )
    def test_not_broken(self, item):
        assert has_broken_text(item) is False


class TestClampConfidence:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (0.25, 0.25),
            (0, 0.0),
            (1, 1.0),
            (7, 1.0),
            (-3, 0.0),
            (1.5, 1.0),
            (-0.5, 0.0),
            (math.inf, 1.0),
            (-math.inf, 0.0),
            (math.nan, 0.5),
            (True, 0.5),
            (False, 0.5),
            # Past float range: compared exactly, no OverflowError.
            (10**400, 1.0),
            (-(10**400), 0.0),
        ],
    )
    def test_clamps(self, value, expected):
        result = clamp_confidence(value)
        assert result == expected
        assert isinstance(result, float)


class TestTokenCount:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (12, 12),
            (0, 0),
            (-5, 0),
            (MAX_TOKEN_COUNT + 1, MAX_TOKEN_COUNT),
            (True, 0),
            (None, 0),
            ("12", 0),
            (1.5, 0),
        ],
    )
    def test_count(self, value, expected):
        assert token_count(value) == expected


def _book(title: str, author: str = "A") -> BookCandidate:
    return BookCandidate(title=title, author=author, confidence=0.5)


def _collect(items, max_books=10):
    return collect_candidates(
        items,
        max_books,
        is_broken=lambda item: item is None,
        to_candidate=lambda item: _book(item) if item else None,
        dedupe_key=lambda c: c.title.casefold(),
    )


class TestCollectCandidates:
    def test_keeps_order_and_counts_broken(self):
        candidates, broken = _collect(["A", None, "B", None])
        assert [c.title for c in candidates] == ["A", "B"]
        assert broken == 2

    def test_dropped_items_are_not_broken(self):
        candidates, broken = _collect(["", "A", ""])
        assert [c.title for c in candidates] == ["A"]
        assert broken == 0

    def test_duplicates_use_the_dedupe_key(self):
        candidates, _ = _collect(["Dune", "DUNE", "dune", "Emma"])
        assert [c.title for c in candidates] == ["Dune", "Emma"]

    def test_stops_at_max_books_without_reading_the_rest(self):
        seen = []

        def is_broken(item):
            seen.append(item)
            return item is None

        candidates, broken = collect_candidates(
            ["A", "B", None, "C"],
            2,
            is_broken=is_broken,
            to_candidate=_book,
            dedupe_key=lambda c: c.title,
        )
        assert [c.title for c in candidates] == ["A", "B"]
        # The broken item after the cap is never looked at.
        assert broken == 0
        assert seen == ["A", "B"]

    def test_empty(self):
        assert _collect([]) == ([], 0)


# Recorded replies -> (Claude's candidates, OpenAI's candidates) as
# (title, author, confidence, isbn_13, isbn_10); None is malformed. Every
# expected value is what each adapter's own parser returned on ``dev``
# before the helpers moved to ``_common`` (#814), so the move changed nothing.
_ISBN_13 = "9780306406157"
_ISBN_10 = "0306406152"
ADAPTER_REPLIES = {
    "two_books_with_isbns": (
        {
            "books": [
                {
                    "title": "Dune",
                    "author": "Frank Herbert",
                    "confidence": 0.9,
                    "isbn_13": "978-0-306-40615-7",
                    "isbn_10": "0 306 40615 2",
                },
                {"title": "Emma", "author": "Jane Austen", "confidence": 0.4},
            ]
        },
        [
            ("Dune", "Frank Herbert", 0.9, _ISBN_13, _ISBN_10),
            ("Emma", "Jane Austen", 0.4, None, None),
        ],
        [
            ("Dune", "Frank Herbert", 0.9, _ISBN_13, _ISBN_10),
            ("Emma", "Jane Austen", 0.4, None, None),
        ],
    ),
    "invented_isbns_dropped_book_kept": (
        {
            "books": [
                {
                    "title": "Dune",
                    "author": "Frank Herbert",
                    "confidence": 0.8,
                    "isbn_13": "9780786412345",
                    "isbn_10": "0306406153",
                }
            ]
        },
        [("Dune", "Frank Herbert", 0.8, None, None)],
        [("Dune", "Frank Herbert", 0.8, None, None)],
    ),
    "confidence_clamped": (
        {
            "books": [
                {"title": "A", "author": "X", "confidence": 7},
                {"title": "B", "author": "X", "confidence": -0.5},
                {"title": "C", "author": "X", "confidence": True},
                {"title": "D", "author": "X"},
            ]
        },
        [
            ("A", "X", 1.0, None, None),
            ("B", "X", 0.0, None, None),
            ("C", "X", 0.5, None, None),
            ("D", "X", 0.5, None, None),
        ],
        [
            ("A", "X", 1.0, None, None),
            ("B", "X", 0.0, None, None),
            ("C", "X", 0.5, None, None),
            ("D", "X", 0.5, None, None),
        ],
    ),
    # #795, intentionally unresolved: Claude gives any non-number 0.5;
    # OpenAI parses a numeric string and treats any other non-number as
    # broken.
    "non_numeric_confidence_795": (
        {
            "books": [
                {"title": "A", "author": "X", "confidence": "0.9"},
                {"title": "B", "author": "X", "confidence": "high"},
                {"title": "C", "author": "X", "confidence": None},
            ]
        },
        [
            ("A", "X", 0.5, None, None),
            ("B", "X", 0.5, None, None),
            ("C", "X", 0.5, None, None),
        ],
        [("A", "X", 0.9, None, None)],
    ),
    "only_non_numeric_confidence_795": (
        {"books": [{"title": "B", "author": "X", "confidence": "high"}]},
        [("B", "X", 0.5, None, None)],
        None,
    ),
    "duplicates": (
        {
            "books": [
                {"title": "Dune", "author": "Frank Herbert", "confidence": 0.9},
                {"title": "DUNE", "author": "frank herbert", "confidence": 0.2},
                {"title": " Dune ", "author": "Frank Herbert", "confidence": 0.3},
            ]
        },
        [("Dune", "Frank Herbert", 0.9, None, None)],
        [("Dune", "Frank Herbert", 0.9, None, None)],
    ),
}


def _rows(candidates: list[BookCandidate] | None):
    if candidates is None:
        return None
    return [(c.title, c.author, c.confidence, c.isbn_13, c.isbn_10) for c in candidates]


def _claude(reply: dict, max_books: int = 20):
    return _rows(parse_reply(json.dumps(reply), "end_turn", max_books))


def _openai(reply: dict, max_books: int = 20):
    provider = OpenAIVisionProvider(FEATURES["scan.standard"])
    return _rows(provider._parse_reply(json.dumps(reply), "stop", max_books))


@pytest.mark.parametrize(
    ("reply", "claude_expected", "openai_expected"),
    ADAPTER_REPLIES.values(),
    ids=ADAPTER_REPLIES,
)
class TestAdaptersUnchanged:
    def test_claude(self, reply, claude_expected, openai_expected):
        assert _claude(reply) == claude_expected

    def test_openai(self, reply, claude_expected, openai_expected):
        assert _openai(reply) == openai_expected

    def test_max_books_caps_both(self, reply, claude_expected, openai_expected):
        assert _claude(reply, 1) == claude_expected[:1]
        if openai_expected is not None:
            assert _openai(reply, 1) == openai_expected[:1]

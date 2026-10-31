"""#754: the OpenAI adapter clamps confidence and caps title/author length,
like the Claude adapter. HTTP is mocked; no real model is called."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai.providers.anthropic_vision import parse_reply
from app.ai.providers.openai_vision import OpenAIVisionProvider
from app.ai.registry import FEATURES


def _item(**changes):
    return {"title": "Dune", "author": "Frank Herbert", "confidence": 0.9, **changes}


async def _identify(items):
    body = {
        "choices": [
            {
                "message": {"content": json.dumps({"books": items})},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = body
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    with patch("app.ai.providers.openai_vision.httpx.AsyncClient", return_value=client):
        return await OpenAIVisionProvider(FEATURES["scan.standard"]).identify(b"img")


@pytest.mark.parametrize(
    "value,expected",
    [
        (1.5, 1.0),
        (-3, 0.0),
        ("nan", 0.5),
        (float("nan"), 0.5),
        ("inf", 1.0),
        ("-inf", 0.0),
        (True, 0.5),
        (0.25, 0.25),
    ],
)
async def test_confidence_is_clamped(value, expected):
    # json.dumps writes a float NaN as the bare token NaN, which json.loads
    # reads back, as a real reply could.
    result = await _identify([_item(confidence=value)])
    assert result.outcome == "ok"
    assert [c.confidence for c in result.candidates] == [expected]


async def test_non_numeric_confidence_is_broken():
    result = await _identify([_item(confidence="high")])
    assert (result.outcome, result.candidates) == ("malformed", [])


@pytest.mark.parametrize("field", ["title", "author"])
async def test_501_characters_is_broken(field):
    result = await _identify([_item(**{field: "x" * 501})])
    assert (result.outcome, result.candidates) == ("malformed", [])


@pytest.mark.parametrize("field", ["title", "author"])
async def test_501_characters_dropped_beside_a_good_item(field):
    result = await _identify([_item(**{field: "x" * 501}), _item(title="Emma")])
    assert result.outcome == "ok"
    assert [c.title for c in result.candidates] == ["Emma"]


@pytest.mark.parametrize("field", ["title", "author"])
async def test_exactly_500_characters_is_accepted(field):
    result = await _identify([_item(**{field: "x" * 500})])
    assert result.outcome == "ok"
    assert len(getattr(result.candidates[0], field)) == 500


async def test_numeric_string_confidence_parses_and_clamps():
    result = await _identify(
        [_item(confidence="1.5"), _item(title="Emma", confidence=" 0.9 ")]
    )
    assert [c.confidence for c in result.candidates] == [1.0, 0.9]


def _huge_reply(literal: str) -> str:
    # Raw JSON text: json.loads turns the digits into an int past float range.
    return (
        '{"books":[{"title":"Dune","author":"Frank Herbert","confidence":'
        + literal
        + "}]}"
    )


HUGE = [("1" + "0" * 399, 1.0), ("-1" + "0" * 399, 0.0)]


@pytest.mark.parametrize("literal,expected", HUGE)
def test_openai_huge_integer_confidence_clamps(literal, expected):
    provider = OpenAIVisionProvider(FEATURES["scan.standard"])
    candidates = provider._parse_reply(_huge_reply(literal), "stop", 15)
    assert [c.confidence for c in candidates] == [expected]


@pytest.mark.parametrize("literal,expected", HUGE)
def test_claude_huge_integer_confidence_clamps(literal, expected):
    candidates = parse_reply(_huge_reply(literal), "end_turn", 15)
    assert [c.confidence for c in candidates] == [expected]

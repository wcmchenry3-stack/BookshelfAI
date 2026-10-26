"""Spend-cap image-token bounds (#536 review): checked against values worked
out by hand from the providers' published formulas, not from the module's
own constants.

- Anthropic vision docs: tokens = (width x height) / 750.
- OpenAI vision docs, detail "high": fit within 2048 x 2048, scale the
  shortest side to 768, count 512 px tiles; gpt-4o 85 + 170 per tile (their
  worked examples: 1024 x 1024 -> 765, 2048 x 4096 -> 1105), gpt-4o-mini
  2,833 + 5,667 per tile.
"""

import pytest

from app.ai.models_allowlist import ALLOWED_MODELS
from eval.ai_scan import EvalError, runner
from eval.ai_scan import image_tokens as it


@pytest.mark.parametrize(
    "model, w, h, expected",
    [
        # Claude: ceil(w * h / 750)
        ("claude-haiku-5-5", 1568, 1568, 3279),  # 2,458,624 / 750 = 3278.2
        ("claude-sonnet-5-5", 1568, 1176, 2459),  # 1,843,968 / 750 = 2458.6
        ("claude-haiku-5-5", 750, 1, 1),
        # gpt-4o, OpenAI's own worked examples
        ("gpt-4o", 1024, 1024, 765),  # -> 768 x 768, 4 tiles
        ("gpt-4o", 2048, 4096, 1105),  # -> 1024 x 2048 -> 768 x 1536, 6 tiles
        ("gpt-4o-2024-11-20", 1568, 768, 85 + 170 * 8),  # 4 x 2 tiles
        # gpt-4o-mini: same tiles, 2,833 + 5,667 per tile
        ("gpt-4o-mini", 1024, 1024, 2833 + 5667 * 4),
        ("gpt-4o-mini", 1568, 768, 2833 + 5667 * 8),  # = 48,169
    ],
)
def test_image_tokens_match_the_published_formulas(model, w, h, expected):
    assert it.image_tokens(model, w, h) == expected


def test_short_side_under_768_takes_the_larger_reading():
    # 1568 x 392: downscale-only reading 4 x 1 tiles; scaled up to a 768
    # shortest side and fitted back into 2048: 2048 x 512, also 4 x 1.
    assert it.image_tokens("gpt-4o", 1568, 392) == 85 + 170 * 4
    # 600 x 500: downscale-only 2 x 1 = 2 tiles; scaled up: 922 x 768, 2 x 2.
    assert it.image_tokens("gpt-4o", 600, 500) == 85 + 170 * 4
    # 512 x 512: 1 tile without upscaling (2,833 + 5,667 = 8,500), 4 tiles
    # scaled up to 768 x 768; the bound never goes below either reading.
    tokens = it.image_tokens("gpt-4o-mini-2024-07-18", 512, 512)
    assert tokens >= 2833 + 5667
    assert tokens == 2833 + 5667 * 4


@pytest.mark.parametrize(
    "model, expected",
    [
        ("gpt-4o-mini", 2833 + 5667 * 8),  # e.g. 1568 x 769 -> 1566 x 768
        ("gpt-4o", 85 + 170 * 8),
        ("claude-sonnet-5-5", 3279),  # the square 1568 x 1568
    ],
)
def test_worst_case_at_the_max_long_edge(model, expected):
    assert it.worst_case_image_tokens(model, 1568) == expected


def test_every_allow_listed_model_has_a_rule():
    for models in ALLOWED_MODELS.values():
        for model in models:
            it.rule_for(model)


def test_a_model_without_a_rule_is_refused(monkeypatch):
    rules = dict(it.IMAGE_TOKEN_RULES)
    del rules["gpt-4o-mini"]
    monkeypatch.setattr(it, "IMAGE_TOKEN_RULES", rules)
    with pytest.raises(EvalError, match="no image-token rule"):
        runner.resolve_spec("scan.standard", "gpt-4o-mini")


def test_no_pixels_is_an_error():
    with pytest.raises(ValueError):
        it.image_tokens("gpt-4o", 0, 10)

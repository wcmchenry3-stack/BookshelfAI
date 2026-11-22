"""Spend-cap image-token bounds (#536 review): checked against values worked
out by hand from the published formula, not from the module's own
constants.

- Anthropic vision docs: tokens = (width x height) / 750.
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
    ],
)
def test_image_tokens_match_the_published_formulas(model, w, h, expected):
    assert it.image_tokens(model, w, h) == expected


@pytest.mark.parametrize(
    "model, expected",
    [
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
    del rules["claude-haiku-5-5"]
    monkeypatch.setattr(it, "IMAGE_TOKEN_RULES", rules)
    with pytest.raises(EvalError, match="no image-token rule"):
        runner.resolve_spec("scan.standard", "claude-haiku-5-5")


def test_no_pixels_is_an_error():
    with pytest.raises(ValueError):
        it.image_tokens("claude-haiku-5-5", 0, 10)

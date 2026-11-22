"""Upper bounds on the input tokens one image costs, per model.

Used only by the eval's spend cap, so every rule errs high. A model with no
rule here is refused (fail closed) rather than estimated.

Anthropic (Claude vision docs): about ``width * height / 750`` tokens. An
image larger than the model's limit is first scaled down, which only lowers
the count, so ``ceil(w * h / 750)`` of the image we send is an upper bound.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class AreaRule:
    """Tokens = ceil(w * h / pixels_per_token)."""

    pixels_per_token: int


_CLAUDE = AreaRule(pixels_per_token=750)

IMAGE_TOKEN_RULES: MappingProxyType[str, AreaRule] = MappingProxyType(
    {
        "claude-haiku-5-5": _CLAUDE,
        "claude-sonnet-5-5": _CLAUDE,
    }
)


class NoImageTokenRule(LookupError):
    """The model has no image-token rule: its cost cannot be bounded."""


def rule_for(model: str) -> AreaRule:
    try:
        return IMAGE_TOKEN_RULES[model]
    except KeyError:
        raise NoImageTokenRule(model) from None


def image_tokens(model: str, width: int, height: int) -> int:
    """An upper bound on the tokens of one ``width`` x ``height`` image."""
    if width <= 0 or height <= 0:
        raise ValueError("image has no pixels")
    rule = rule_for(model)
    return math.ceil(width * height / rule.pixels_per_token)


def worst_case_image_tokens(model: str, max_long_edge_px: int) -> int:
    """The bound for any image whose long edge is at most
    ``max_long_edge_px`` (when the dimensions are not known yet)."""
    rule_for(model)
    return max(
        image_tokens(model, max_long_edge_px, short)
        for short in range(1, max_long_edge_px + 1)
    )

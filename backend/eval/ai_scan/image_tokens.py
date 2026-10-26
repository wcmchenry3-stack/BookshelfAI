"""Upper bounds on the input tokens one image costs, per model.

Used only by the eval's spend cap, so every rule errs high. A model with no
rule here is refused (fail closed) rather than estimated.

- Anthropic (Claude vision docs): about ``width * height / 750`` tokens. An
  image larger than the model's limit is first scaled down, which only
  lowers the count, so ``ceil(w * h / 750)`` of the image we send is an upper
  bound.
- OpenAI, ``detail: "high"`` (the adapter's setting; OpenAI vision docs):
  scale to fit within 2048 x 2048, then scale so the shortest side is 768 px,
  then count 512 px tiles; tokens = base + per_tile * tiles (gpt-4o: 85 +
  170, gpt-4o-mini: 2,833 + 5,667). The docs do not say whether a shortest
  side under 768 is scaled UP, so the bound takes the larger tile count of
  "downscale only" and "scale the shortest side to exactly 768, then fit
  within 2048 x 2048 again".
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class AreaRule:
    """Tokens = ceil(w * h / pixels_per_token)."""

    pixels_per_token: int


@dataclass(frozen=True)
class TileRule:
    """OpenAI high detail: base + per_tile * 512 px tiles."""

    base: int
    per_tile: int


_CLAUDE = AreaRule(pixels_per_token=750)
_GPT_4O = TileRule(base=85, per_tile=170)
_GPT_4O_MINI = TileRule(base=2_833, per_tile=5_667)

IMAGE_TOKEN_RULES: MappingProxyType[str, AreaRule | TileRule] = MappingProxyType(
    {
        "claude-haiku-5-5": _CLAUDE,
        "claude-sonnet-5-5": _CLAUDE,
        "gpt-4o": _GPT_4O,
        "gpt-4o-2024-08-06": _GPT_4O,
        "gpt-4o-2024-11-20": _GPT_4O,
        "gpt-4o-mini": _GPT_4O_MINI,
        "gpt-4o-mini-2024-07-18": _GPT_4O_MINI,
    }
)


class NoImageTokenRule(LookupError):
    """The model has no image-token rule: its cost cannot be bounded."""


def rule_for(model: str) -> AreaRule | TileRule:
    try:
        return IMAGE_TOKEN_RULES[model]
    except KeyError:
        raise NoImageTokenRule(model) from None


def _scale(w: float, h: float, factor: float) -> tuple[float, float]:
    return w * factor, h * factor


def _tiles(w: float, h: float) -> int:
    return math.ceil(w / 512) * math.ceil(h / 512)


def openai_high_detail_tiles(width: int, height: int) -> int:
    w, h = float(width), float(height)
    if max(w, h) > 2048:
        w, h = _scale(w, h, 2048 / max(w, h))
    down_only = _scale(w, h, 768 / min(w, h)) if min(w, h) > 768 else (w, h)
    up_w, up_h = _scale(w, h, 768 / min(w, h))
    if max(up_w, up_h) > 2048:
        up_w, up_h = _scale(up_w, up_h, 2048 / max(up_w, up_h))
    return max(_tiles(*down_only), _tiles(up_w, up_h))


def image_tokens(model: str, width: int, height: int) -> int:
    """An upper bound on the tokens of one ``width`` x ``height`` image."""
    if width <= 0 or height <= 0:
        raise ValueError("image has no pixels")
    rule = rule_for(model)
    if isinstance(rule, AreaRule):
        return math.ceil(width * height / rule.pixels_per_token)
    return rule.base + rule.per_tile * openai_high_detail_tiles(width, height)


def worst_case_image_tokens(model: str, max_long_edge_px: int) -> int:
    """The bound for any image whose long edge is at most
    ``max_long_edge_px`` (when the dimensions are not known yet)."""
    rule_for(model)
    return max(
        image_tokens(model, max_long_edge_px, short)
        for short in range(1, max_long_edge_px + 1)
    )

"""List prices for the models the gateway may call (#530).

The usage ledger (``app.ai.ledger``) prices every provider call from this
table, keyed by ``(model, price_version)``. Each row stores the version it was
priced under, so a price change is a new version: add rows under a new
``CURRENT_PRICE_VERSION`` and keep the old rows, so old ledger rows can still
be explained.

Prices are public list prices, not secrets. They are in micro-dollars (one US
dollar is 1,000,000 micros) per one million tokens, so they stay integers.

Every model in ``ALLOWED_MODELS`` must have a row under
``CURRENT_PRICE_VERSION``: checked when this module is imported (so the app
does not start) and by ``tests/unit/test_ai_prices.py``, so a model that
cannot be priced fails CI or boot, never a paid call.
An unknown ``(model, price_version)`` raises :class:`UnknownPriceError`: a
call is never silently priced at zero.
"""

from dataclasses import dataclass
from types import MappingProxyType

from app.ai.models_allowlist import ALLOWED_MODELS


class UnknownPriceError(LookupError):
    """No price row for this ``(model, price_version)``."""


@dataclass(frozen=True)
class ModelPrice:
    """Micro-dollars per one million tokens of each kind."""

    input: int
    output: int
    cache_read: int
    cache_write: int

    def __post_init__(self) -> None:
        for name in ("input", "output", "cache_read", "cache_write"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"price {name}={value!r} must be a non-negative int")


TOKENS_PER_PRICE_UNIT = 1_000_000

CURRENT_PRICE_VERSION = "2026-10"

# Source: OpenAI API pricing page (https://openai.com/api/pricing), standard
# tier, per 1M tokens. Verify against the pricing page before changing; a
# change is a new price_version, never an edit of an existing row.
#   gpt-4o-mini: input $0.15, cached input $0.075, output $0.60
#   gpt-4o:      input $2.50, cached input $1.25,  output $10.00
# OpenAI does not bill prompt-cache writes separately, so cache_write is 0.
_GPT_4O_MINI = ModelPrice(
    input=150_000, output=600_000, cache_read=75_000, cache_write=0
)
_GPT_4O = ModelPrice(
    input=2_500_000, output=10_000_000, cache_read=1_250_000, cache_write=0
)

PRICES: MappingProxyType[tuple[str, str], ModelPrice] = MappingProxyType(
    {
        ("gpt-4o-mini", "2026-10"): _GPT_4O_MINI,
        ("gpt-4o-mini-2024-07-18", "2026-10"): _GPT_4O_MINI,
        ("gpt-4o", "2026-10"): _GPT_4O,
        ("gpt-4o-2024-08-06", "2026-10"): _GPT_4O,
        ("gpt-4o-2024-11-20", "2026-10"): _GPT_4O,
    }
)


def unpriced_models(price_version: str = CURRENT_PRICE_VERSION) -> set[str]:
    """Allow-listed models with no price row under ``price_version``."""
    allowed = {m for models in ALLOWED_MODELS.values() for m in models}
    return {m for m in allowed if (m, price_version) not in PRICES}


# Checked at import (the app imports this module through the ledger), so a
# model allowed without a price stops the app from starting.
if _missing := unpriced_models():
    raise RuntimeError(
        f"allow-listed models without a {CURRENT_PRICE_VERSION} price: "
        f"{sorted(_missing)}"
    )


def price_for(model: str, price_version: str) -> ModelPrice:
    try:
        return PRICES[(model, price_version)]
    except KeyError:
        raise UnknownPriceError(
            f"no price for model {model!r} at price_version {price_version!r}"
        ) from None


def _count(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name}={value!r} must be a non-negative int")
    return value


def cost_micros(
    model: str,
    price_version: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> int:
    """The cost of one call in micro-dollars.

    Integer arithmetic only: each token count times its per-million price,
    summed, then divided by one million and rounded UP to the next whole
    micro-dollar (so a call is never under-counted, and the rounding error is
    under one micro-dollar per call). The token counts are disjoint (see
    ``ProviderResult``). Raises :class:`UnknownPriceError` for an unknown
    model or version, and ``ValueError`` for a negative or non-int count.
    """
    price = price_for(model, price_version)
    scaled = (
        _count("input_tokens", input_tokens) * price.input
        + _count("output_tokens", output_tokens) * price.output
        + _count("cache_read_tokens", cache_read_tokens) * price.cache_read
        + _count("cache_write_tokens", cache_write_tokens) * price.cache_write
    )
    return -(-scaled // TOKENS_PER_PRICE_UNIT)

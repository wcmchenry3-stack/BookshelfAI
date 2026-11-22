"""Price table and cost arithmetic (#530)."""

from unittest.mock import patch

import pytest

from app.ai import prices
from app.ai.models_allowlist import ALLOWED_MODELS
from app.ai.prices import (
    CURRENT_PRICE_VERSION,
    PRICES,
    ModelPrice,
    UnknownPriceError,
    cost_micros,
)
from app.ai.registry import FEATURES

V = CURRENT_PRICE_VERSION


class TestCostArithmetic:
    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            # 1,000 input  * 100,000 + 500 output * 500,000
            # + 2,000 cached * 10,000 = 370,000,000 -> 370 micros
            ("claude-haiku-5-5", 370),
            # 1,000 * 2,000,000 + 500 * 10,000,000 + 2,000 * 200,000
            # = 7,400,000,000 -> 7,400 micros
            ("claude-sonnet-5-5", 7_400),
        ],
    )
    def test_known_tokens_give_known_cost(self, model, expected):
        assert (
            cost_micros(
                model,
                V,
                input_tokens=1_000,
                output_tokens=500,
                cache_read_tokens=2_000,
                cache_write_tokens=0,
            )
            == expected
        )

    def test_each_token_kind_uses_its_own_price(self):
        m = "claude-sonnet-5-5"
        assert cost_micros(m, V, input_tokens=1_000_000) == 2_000_000
        assert cost_micros(m, V, output_tokens=1_000_000) == 10_000_000
        assert cost_micros(m, V, cache_read_tokens=1_000_000) == 200_000
        assert cost_micros(m, V, cache_write_tokens=1_000_000) == 2_500_000

    def test_rounds_up_to_the_next_micro(self):
        # 1 input token of Haiku 5.5 is 0.1 micros: billed as 1.
        assert cost_micros("claude-haiku-5-5", V, input_tokens=1) == 1
        # 11 tokens = 1.1 micros -> 2.
        assert cost_micros("claude-haiku-5-5", V, input_tokens=11) == 2
        # Exactly 1 micro is not rounded further.
        assert cost_micros("claude-haiku-5-5", V, input_tokens=10) == 1
        assert cost_micros("claude-sonnet-5-5", V, output_tokens=1) == 10

    def test_zero_tokens_cost_zero(self):
        assert cost_micros("claude-sonnet-5-5", V) == 0

    def test_large_counts_stay_exact_integers(self):
        cost = cost_micros(
            "claude-sonnet-5-5", V, input_tokens=10**9, output_tokens=10**9
        )
        assert cost == 12_000_000_000
        assert isinstance(cost, int)


class TestFailsLoudly:
    def test_unknown_price_version_raises(self):
        with pytest.raises(UnknownPriceError, match="1999-01"):
            cost_micros("claude-sonnet-5-5", "1999-01", input_tokens=10)

    def test_unknown_price_version_raises_even_with_zero_tokens(self):
        """Not silently zero: the lookup fails before any arithmetic."""
        with pytest.raises(UnknownPriceError):
            cost_micros("claude-sonnet-5-5", "no-such-version")

    def test_unknown_model_raises(self):
        with pytest.raises(UnknownPriceError, match="claude-9"):
            cost_micros("claude-9", V, input_tokens=10)

    @pytest.mark.parametrize("bad", [-1, 1.5, "10", True, None])
    def test_bad_token_count_raises(self, bad):
        with pytest.raises(ValueError):
            cost_micros("claude-sonnet-5-5", V, input_tokens=bad)

    @pytest.mark.parametrize("bad", [-1, 1.5, True])
    def test_bad_price_row_rejected(self, bad):
        with pytest.raises(ValueError):
            ModelPrice(input=bad, output=0, cache_read=0, cache_write=0)


class TestCoverage:
    def test_every_allow_listed_model_has_a_current_price(self):
        allowed = {m for models in ALLOWED_MODELS.values() for m in models}
        missing = {m for m in allowed if (m, V) not in PRICES}
        assert missing == set()
        assert prices.unpriced_models() == set()

    def test_every_registry_default_has_a_current_price(self):
        for spec in FEATURES.values():
            assert (spec.model, V) in PRICES, spec.name

    def test_unpriced_model_is_detected(self):
        with patch.object(
            prices,
            "ALLOWED_MODELS",
            {"anthropic": frozenset({"claude-sonnet-5-5", "claude-new"})},
        ):
            assert prices.unpriced_models() == {"claude-new"}

    def test_table_is_read_only(self):
        with pytest.raises(TypeError):
            PRICES[("claude-sonnet-5-5", V)] = ModelPrice(0, 0, 0, 0)  # type: ignore[index]


class TestRetiredProviderPrices:
    def test_only_anthropic_models_are_priced(self):
        assert {m for (m, _v) in PRICES} == set(ALLOWED_MODELS["anthropic"])

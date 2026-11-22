"""Switching a feature's provider (#532): registry per-provider models, the
``switch`` CLI that writes provider and model together, and the price-card
guard."""

import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.dialects import postgresql

from app.ai import prices, registry
from app.ai import settings as ai_settings
from app.ai.registry import (
    FEATURES,
    PROVIDER_MODELS,
    FeatureSpecError,
    default_model,
)

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestProviderModels:
    def test_each_scan_feature_has_a_model_per_provider(self):
        assert set(PROVIDER_MODELS) == set(FEATURES)
        for models in PROVIDER_MODELS.values():
            assert set(models) == {"anthropic"}

    def test_anthropic_models_are_the_registry_defaults(self):
        assert default_model("scan.standard", "anthropic") == (
            FEATURES["scan.standard"].model
        )
        assert default_model("scan.enhanced", "anthropic") == (
            FEATURES["scan.enhanced"].model
        )

    def test_unknown_provider_has_no_default(self):
        with pytest.raises(FeatureSpecError):
            default_model("scan.standard", "gemini")

    def test_import_time_check_rejects_a_bad_model(self):
        bad = {"scan.standard": {"anthropic": "claude-x"}}
        with (
            patch.object(registry, "PROVIDER_MODELS", bad),
            pytest.raises(FeatureSpecError),
        ):
            registry._validate_provider_models()

    def test_import_time_check_rejects_disagreement_with_the_default(self):
        bad = {"scan.standard": {"anthropic": "claude-sonnet-5-5"}}
        with (
            patch.object(registry, "PROVIDER_MODELS", bad),
            pytest.raises(FeatureSpecError, match="disagrees"),
        ):
            registry._validate_provider_models()


# ---------------------------------------------------------------------------
# ai_settings: reading an anthropic pair
# ---------------------------------------------------------------------------


class TestReadingAnthropicRows:
    def test_anthropic_pair_is_valid_on_read(self):
        rows = {
            "scan.standard.provider": "anthropic",
            "scan.standard.model": "claude-haiku-5-5",
        }
        ai_settings.reset_cache(rows)
        try:
            assert dict(ai_settings._cache.rows) == rows
            view = ai_settings.AISettings(ai_settings._cache.rows)
            spec = view.spec_for(FEATURES["scan.standard"])
        finally:
            ai_settings.reset_cache()
        assert (spec.provider, spec.model) == ("anthropic", "claude-haiku-5-5")
        # Everything else stays the registry's.
        assert spec == FEATURES["scan.standard"]

    def test_anthropic_provider_with_a_foreign_model_falls_back(self):
        current = ai_settings.AISettings(
            {
                "scan.standard.provider": "anthropic",
                "scan.standard.model": "other-model",
            }
        )
        spec = current.spec_for(FEATURES["scan.standard"])
        assert spec == FEATURES["scan.standard"]


# ---------------------------------------------------------------------------
# validate_switch / switch CLI
# ---------------------------------------------------------------------------


class TestValidateSwitch:
    @pytest.mark.parametrize(
        ("feature", "provider", "model"),
        [
            ("scan.standard", "anthropic", "claude-haiku-5-5"),
            ("scan.enhanced", "anthropic", "claude-sonnet-5-5"),
        ],
    )
    def test_default_model_per_provider(self, feature, provider, model):
        assert ai_settings.validate_switch(feature, provider) == (provider, model)

    def test_explicit_model(self):
        assert ai_settings.validate_switch(
            "scan.standard", "anthropic", "claude-sonnet-5-5"
        ) == ("anthropic", "claude-sonnet-5-5")

    @pytest.mark.parametrize(
        ("feature", "provider", "model"),
        [
            ("scan.standard", "anthropic", "other-model"),
            ("scan.standard", "other-provider", "claude-haiku-5-5"),
            ("scan.standard", "anthropic", "claude-haiku-5-5-latest"),
            ("scan.standard", "gemini", None),
            ("scan.premium", "anthropic", None),
        ],
    )
    def test_invalid_switch_refused(self, feature, provider, model):
        with pytest.raises(ai_settings.AISettingValueError):
            ai_settings.validate_switch(feature, provider, model)


@pytest.fixture
def cli_db():
    db = AsyncMock()

    @asynccontextmanager
    async def _session():
        yield db

    with (
        patch.object(ai_settings, "_open_session", _session),
        patch.object(ai_settings, "read_rows", AsyncMock(return_value={})),
        patch.object(ai_settings, "_dispose_engine", new=AsyncMock()),
    ):
        yield db


def _writes(db) -> list[dict]:
    return [
        call.args[0].compile(dialect=postgresql.dialect()).params
        for call in db.execute.await_args_list
    ]


class TestSwitchCLI:
    def test_switch_writes_provider_and_model_in_one_commit(self, cli_db, capsys):
        argv = ["switch", "scan.standard", "anthropic", "--by", "ops"]
        assert ai_settings.main(argv) == 0
        writes = _writes(cli_db)
        assert [(w["key"], w["value"]) for w in writes] == [
            ("scan.standard.provider", "anthropic"),
            ("scan.standard.model", "claude-haiku-5-5"),
        ]
        assert {w["updated_by"] for w in writes} == {"ops"}
        cli_db.commit.assert_awaited_once()
        out = capsys.readouterr().out
        assert '"anthropic"' in out and '"claude-haiku-5-5"' in out

    def test_switch_with_model(self, cli_db):
        argv = ["switch", "scan.standard", "anthropic", "--model", "claude-sonnet-5-5"]
        assert ai_settings.main(argv) == 0
        assert _writes(cli_db)[1]["value"] == "claude-sonnet-5-5"

    @pytest.mark.parametrize(
        "argv",
        [
            ["switch", "scan.standard", "anthropic", "--model", "other-model"],
            ["switch", "scan.standard", "anthropic", "--model", "claude-3"],
        ],
    )
    def test_bad_switch_touches_nothing(self, cli_db, argv, capsys):
        assert ai_settings.main(argv) == 2
        cli_db.execute.assert_not_awaited()
        assert "error:" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "argv",
        [
            ["switch", "scan.standard", "gemini"],
            ["switch", "scan.premium", "anthropic"],
        ],
    )
    def test_unknown_choices_rejected(self, cli_db, argv):
        with pytest.raises(SystemExit):
            ai_settings.main(argv)
        cli_db.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# Price card guard
# ---------------------------------------------------------------------------


class TestLongPromptGuard:
    def test_haiku_above_100k_warns(self, caplog):
        with caplog.at_level(logging.WARNING, logger=prices.__name__):
            prices.cost_micros(
                "claude-haiku-5-5",
                prices.CURRENT_PRICE_VERSION,
                input_tokens=90_000,
                cache_read_tokens=10_001,
            )
        assert "under-counted" in caplog.text

    def test_normal_scan_prompt_does_not_warn(self, caplog):
        with caplog.at_level(logging.WARNING, logger=prices.__name__):
            prices.cost_micros(
                "claude-haiku-5-5", prices.CURRENT_PRICE_VERSION, input_tokens=3_000
            )
            prices.cost_micros(
                "claude-sonnet-5-5", prices.CURRENT_PRICE_VERSION, input_tokens=500_000
            )
        assert caplog.text == ""

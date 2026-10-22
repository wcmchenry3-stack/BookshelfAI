"""AI feature registry: values, bounds and import-time validation."""

import dataclasses

import pytest

from app.ai.registry import (
    FEATURES,
    FeatureSpec,
    FeatureSpecError,
    UnknownFeatureError,
    build_registry,
    get_feature,
)
from app.core.config import settings


def _spec(**overrides) -> FeatureSpec:
    values = {
        "name": "test.feature",
        "provider": "openai",
        "model": "gpt-4o-mini",
        "effort": "medium",
        "max_tokens": 2048,
        "max_image_long_edge_px": 1568,
        "max_images": 1,
        "timeout_s": 45.0,
        "est_cost_ceiling_micros": 10_000,
    }
    values.update(overrides)
    return FeatureSpec(**values)


class TestRegisteredFeatures:
    def test_has_exactly_the_scan_features(self):
        assert set(FEATURES) == {"scan.standard", "scan.enhanced"}

    def test_keys_match_spec_names(self):
        for name, spec in FEATURES.items():
            assert spec.name == name

    def test_existing_values_are_unchanged(self):
        standard = FEATURES["scan.standard"]
        enhanced = FEATURES["scan.enhanced"]
        assert standard.provider == enhanced.provider == "openai"
        assert standard.model == settings.scan_model == "gpt-4o-mini"
        assert enhanced.model == settings.scan_enhanced_model == "gpt-4o"
        for spec in (standard, enhanced):
            assert spec.max_tokens == settings.scan_max_tokens
            assert spec.timeout_s == settings.scan_timeout_seconds

    def test_new_fields_have_bounded_defaults(self):
        for spec in FEATURES.values():
            assert spec.max_image_long_edge_px == 1568
            assert spec.max_images == 1
            assert spec.enabled is True
            assert spec.effort in {"low", "medium", "high"}
            assert 0 < spec.est_cost_ceiling_micros <= 1_000_000

    def test_registry_is_read_only(self):
        with pytest.raises(TypeError):
            FEATURES["scan.standard"] = _spec()  # type: ignore[index]

    def test_spec_is_frozen(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            FEATURES["scan.standard"].model = "gpt-4o"  # type: ignore[misc]


class TestGetFeature:
    def test_returns_spec(self):
        assert get_feature("scan.enhanced") is FEATURES["scan.enhanced"]

    @pytest.mark.parametrize("name", ["", "scan", "scan.premium", "SCAN.STANDARD"])
    def test_unknown_name_raises(self, name):
        with pytest.raises(UnknownFeatureError):
            get_feature(name)


class TestValidation:
    def test_valid_spec_builds(self):
        assert _spec().enabled is True

    @pytest.mark.parametrize(
        "overrides",
        [
            {"max_tokens": 0},
            {"max_tokens": -1},
            {"max_tokens": 16_385},
            {"max_tokens": True},
            {"max_image_long_edge_px": 0},
            {"max_image_long_edge_px": 10_000},
            {"max_images": 0},
            {"max_images": 11},
            {"timeout_s": 0},
            {"timeout_s": 600},
            {"est_cost_ceiling_micros": 0},
            {"est_cost_ceiling_micros": 1_000_001},
        ],
    )
    def test_out_of_bounds_rejected(self, overrides):
        with pytest.raises(FeatureSpecError):
            _spec(**overrides)

    def test_unknown_provider_rejected(self):
        with pytest.raises(FeatureSpecError, match="unknown provider"):
            _spec(provider="acme")

    @pytest.mark.parametrize("model", ["gpt-5-unknown", "", "claude-model"])
    def test_unknown_model_rejected(self, model):
        with pytest.raises(FeatureSpecError, match="not allowed"):
            _spec(model=model)

    def test_unknown_effort_rejected(self):
        with pytest.raises(FeatureSpecError):
            _spec(effort="max")

    def test_non_bool_enabled_rejected(self):
        with pytest.raises(FeatureSpecError):
            _spec(enabled="yes")

    def test_empty_name_rejected(self):
        with pytest.raises(FeatureSpecError):
            _spec(name="")

    def test_duplicate_feature_rejected(self):
        with pytest.raises(FeatureSpecError, match="duplicate"):
            build_registry([_spec(), _spec()])

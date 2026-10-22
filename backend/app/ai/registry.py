"""Server-controlled registry of AI features.

Each feature has a frozen :class:`FeatureSpec`. Every spec is validated when it
is built, and the registry is built at import, so a bad value (an unknown
provider or model, or an out-of-bounds limit) stops the app from starting
instead of reaching a paid provider.

Values that existed before the gateway still come from ``settings`` for now
(``SCAN_MODEL``, ``SCAN_ENHANCED_MODEL``, ``SCAN_MAX_TOKENS``,
``SCAN_TIMEOUT_SECONDS``), so behaviour is unchanged.
"""

from dataclasses import dataclass, replace
from types import MappingProxyType

from app.ai.models_allowlist import (
    ALLOWED_MODELS,
    MAX_TOKENS_BOUNDS,
    TIMEOUT_S_BOUNDS,
)
from app.core.config import settings


class FeatureSpecError(ValueError):
    """A feature spec has an unknown provider/model or an out-of-bounds value."""


class UnknownFeatureError(LookupError):
    """The requested feature name is not in the registry."""


ALLOWED_EFFORTS = frozenset({"low", "medium", "high"})

# Inclusive bounds: (min, max).
IMAGE_LONG_EDGE_BOUNDS = (256, 4_096)
MAX_IMAGES_BOUNDS = (1, 10)
# One US dollar is 1,000,000 micros.
COST_CEILING_MICROS_BOUNDS = (1, 1_000_000)


def _check_bounds(name: str, field: str, value: float, bounds: tuple) -> None:
    low, high = bounds
    # bool is an int subclass; never accept it as a number.
    if isinstance(value, bool) or not (low <= value <= high):
        raise FeatureSpecError(
            f"{name}: {field}={value!r} is outside the allowed range {low}..{high}"
        )


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    provider: str
    model: str
    effort: str
    max_tokens: int
    max_image_long_edge_px: int
    max_images: int
    timeout_s: float
    est_cost_ceiling_micros: int
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.name:
            raise FeatureSpecError("feature name must not be empty")
        allowed = ALLOWED_MODELS.get(self.provider)
        if allowed is None:
            raise FeatureSpecError(f"{self.name}: unknown provider {self.provider!r}")
        if self.model not in allowed:
            raise FeatureSpecError(
                f"{self.name}: model {self.model!r} is not allowed for "
                f"provider {self.provider!r}"
            )
        if self.effort not in ALLOWED_EFFORTS:
            raise FeatureSpecError(f"{self.name}: unknown effort {self.effort!r}")
        _check_bounds(self.name, "max_tokens", self.max_tokens, MAX_TOKENS_BOUNDS)
        _check_bounds(
            self.name,
            "max_image_long_edge_px",
            self.max_image_long_edge_px,
            IMAGE_LONG_EDGE_BOUNDS,
        )
        _check_bounds(self.name, "max_images", self.max_images, MAX_IMAGES_BOUNDS)
        _check_bounds(self.name, "timeout_s", self.timeout_s, TIMEOUT_S_BOUNDS)
        _check_bounds(
            self.name,
            "est_cost_ceiling_micros",
            self.est_cost_ceiling_micros,
            COST_CEILING_MICROS_BOUNDS,
        )
        if not isinstance(self.enabled, bool):
            raise FeatureSpecError(f"{self.name}: enabled must be a bool")


def build_registry(specs: list[FeatureSpec]) -> MappingProxyType[str, FeatureSpec]:
    """Return a read-only name → spec mapping; duplicate names are rejected."""
    registry: dict[str, FeatureSpec] = {}
    for spec in specs:
        if spec.name in registry:
            raise FeatureSpecError(f"duplicate feature {spec.name!r}")
        registry[spec.name] = spec
    return MappingProxyType(registry)


# max_image_long_edge_px is enforced by the scan path (app.ai.images, #529).
# Not yet enforced: max_images, effort and est_cost_ceiling_micros are declared
# here so later stories (ledger, usage) have one source of truth.
# est_cost_ceiling_micros is a rough upper estimate of one call at list prices
# (image tokens at "high" detail plus a full max_tokens reply).
FEATURES: MappingProxyType[str, FeatureSpec] = build_registry(
    [
        FeatureSpec(
            name="scan.standard",
            provider="openai",
            model=settings.scan_model,
            # Used by the Claude adapter (output_config.effort); the OpenAI
            # adapter ignores it. "low": a standard scan is a high-volume
            # extraction; the enhanced scan keeps "medium" (#532).
            effort="low",
            max_tokens=settings.scan_max_tokens,
            max_image_long_edge_px=1568,
            max_images=1,
            timeout_s=settings.scan_timeout_seconds,
            est_cost_ceiling_micros=10_000,
        ),
        FeatureSpec(
            name="scan.enhanced",
            provider="openai",
            model=settings.scan_enhanced_model,
            effort="medium",
            max_tokens=settings.scan_max_tokens,
            max_image_long_edge_px=1568,
            max_images=1,
            timeout_s=settings.scan_timeout_seconds,
            est_cost_ceiling_micros=30_000,
        ),
    ]
)


# The model each feature uses on each provider. The registry spec above holds
# the default provider (openai until #536/#538); when ai_settings switches a
# feature's provider (``python -m app.ai.settings switch``) the model is taken
# from here unless one is named. Anthropic IDs are fixed IDs, never aliases
# (#548): Haiku 5.5 for standard scans, Sonnet 5.5 for enhanced (#547).
PROVIDER_MODELS: MappingProxyType[str, MappingProxyType[str, str]] = MappingProxyType(
    {
        "scan.standard": MappingProxyType(
            {"openai": settings.scan_model, "anthropic": "claude-haiku-5-5"}
        ),
        "scan.enhanced": MappingProxyType(
            {"openai": settings.scan_enhanced_model, "anthropic": "claude-sonnet-5-5"}
        ),
    }
)


def _validate_provider_models() -> None:
    """Every per-provider model must build a valid spec (allow-list check),
    and the registry default must agree with it. Run at import."""
    for name, models in PROVIDER_MODELS.items():
        spec = FEATURES[name]
        if models.get(spec.provider) != spec.model:
            raise FeatureSpecError(
                f"{name}: PROVIDER_MODELS disagrees with the registry default"
            )
        for provider, model in models.items():
            replace(spec, provider=provider, model=model)


_validate_provider_models()


def default_model(feature: str, provider: str) -> str:
    """The model ``feature`` uses on ``provider`` when none is named.

    Raises ``FeatureSpecError`` when the feature has no model for it."""
    try:
        return PROVIDER_MODELS[feature][provider]
    except KeyError:
        raise FeatureSpecError(
            f"{feature}: no default model for provider {provider!r}"
        ) from None


def get_feature(name: str) -> FeatureSpec:
    try:
        return FEATURES[name]
    except KeyError:
        raise UnknownFeatureError(f"unknown AI feature {name!r}") from None

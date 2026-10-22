"""Server-controlled registry of AI features.

Each feature has a frozen :class:`FeatureSpec`. Every spec is validated when it
is built, and the registry is built at import, so a bad value (an unknown
provider or model, or an out-of-bounds limit) stops the app from starting
instead of reaching a paid provider.

Values that existed before the gateway still come from ``settings`` for now
(``SCAN_MODEL``, ``SCAN_ENHANCED_MODEL``, ``SCAN_MAX_TOKENS``,
``SCAN_TIMEOUT_SECONDS``), so behaviour is unchanged.
"""

from dataclasses import dataclass
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


# New fields not yet enforced by the scan path: max_image_long_edge_px,
# max_images, effort and est_cost_ceiling_micros are declared here so later
# stories (resizing, ledger, usage) have one source of truth.
# est_cost_ceiling_micros is a rough upper estimate of one call at list prices
# (image tokens at "high" detail plus a full max_tokens reply).
FEATURES: MappingProxyType[str, FeatureSpec] = build_registry(
    [
        FeatureSpec(
            name="scan.standard",
            provider="openai",
            model=settings.scan_model,
            effort="medium",
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


def get_feature(name: str) -> FeatureSpec:
    try:
        return FEATURES[name]
    except KeyError:
        raise UnknownFeatureError(f"unknown AI feature {name!r}") from None

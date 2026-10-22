"""The single entry point for paid AI calls.

Callers name a feature; the registry picks provider, model, token cap and
timeout. Nothing from the client reaches those settings.
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.providers.base import VisionProvider
from app.ai.providers.openai_vision import OpenAIVisionProvider
from app.ai.registry import FeatureSpec, get_feature
from app.models.user import User
from app.services.book_identifier import BookCandidate, ScanUnavailableError


class FeatureDisabledError(ScanUnavailableError):
    """The feature is switched off in the registry; no provider was called."""


@dataclass(frozen=True)
class VisionPayload:
    """Input for a vision feature: one sanitised image."""

    image_bytes: bytes


@dataclass(frozen=True)
class AIResult:
    """What a feature call returns. Later stories add usage and cost here."""

    feature: str
    provider: str
    model: str
    candidates: list[BookCandidate]


def _build_provider(spec: FeatureSpec) -> VisionProvider:
    # Looked up at call time (not cached in a dict) so tests can patch the
    # provider class at this module's boundary.
    if spec.provider == "openai":
        return OpenAIVisionProvider(spec)
    # Unreachable while the registry validates providers; fail closed anyway.
    raise ValueError(f"no provider adapter for {spec.provider!r}")


async def run_feature(
    feature: str, *, user: User, db: AsyncSession, payload: VisionPayload
) -> AIResult:
    """Run one AI feature and return its result.

    ``user`` and ``db`` are accepted now so later stories (usage ledger,
    per-user limits) can use them without changing callers.

    Raises ``UnknownFeatureError`` for a name not in the registry,
    ``FeatureDisabledError`` when the feature is switched off, and whatever the
    provider raises (``ScanUnavailableError``, ``httpx.ConnectError``).
    """
    spec = get_feature(feature)
    if not spec.enabled:
        raise FeatureDisabledError(f"AI feature {feature!r} is disabled")
    if not isinstance(payload, VisionPayload):
        raise TypeError(f"{feature} expects a VisionPayload")

    provider = _build_provider(spec)
    candidates = await provider.identify(payload.image_bytes)
    return AIResult(
        feature=spec.name,
        provider=spec.provider,
        model=spec.model,
        candidates=candidates,
    )

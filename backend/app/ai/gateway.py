"""The single entry point for paid AI calls.

Callers name a feature; the registry picks provider, model, token cap and
timeout, and ``ai_settings`` (``app.ai.settings``) may switch the feature off
or override its provider/model within the registry's allow-list. Nothing from
the client reaches those settings.
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import entitlements
from app.ai import settings as ai_settings
from app.ai.entitlements import Tier
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
    tier: Tier


def _build_provider(spec: FeatureSpec) -> VisionProvider:
    # Looked up at call time (not cached in a dict) so tests can patch the
    # provider class at this module's boundary.
    if spec.provider == "openai":
        return OpenAIVisionProvider(spec)
    # Unreachable while the registry validates providers; fail closed anyway.
    raise ValueError(f"no provider adapter for {spec.provider!r}")


async def run_feature(
    feature: str,
    *,
    user: User,
    db: AsyncSession,
    payload: VisionPayload,
    settings_snapshot: ai_settings.AISettings | None = None,
) -> AIResult:
    """Run one AI feature and return its result.

    ``user`` and ``db`` are accepted now so later stories (usage ledger,
    per-user limits) can use them without changing callers.

    The spec used is per request: the registry default with the validated
    ``ai_settings`` provider/model override and kill switch applied
    (``dataclasses.replace``, which re-runs the registry's validation).
    ``settings_snapshot`` is the ``AISettings`` the caller already read for
    this request (``/scan`` reads them first, for its kill-switch checks);
    passing it avoids a second read and makes the whole request use one
    view. Without it the settings are read here. Either way the kill switch
    and overrides are applied here, so this stays the choke point.

    Raises ``UnknownFeatureError`` for a name not in the registry,
    ``FeatureDisabledError`` when the feature is switched off (in the registry
    or in ``ai_settings``), ``AISettingsUnavailableError`` when the settings
    cannot be read and nothing recent enough is cached, and whatever the provider raises
    (``ScanUnavailableError``, ``httpx.ConnectError``).
    """
    spec = get_feature(feature)
    current = (
        settings_snapshot
        if settings_snapshot is not None
        else await ai_settings.get_ai_settings(db)
    )
    spec = current.spec_for(spec)
    if not spec.enabled:
        raise FeatureDisabledError(f"AI feature {feature!r} is disabled")
    if not isinstance(payload, VisionPayload):
        raise TypeError(f"{feature} expects a VisionPayload")

    # Resolved server-side from the ORM user only; looked up on the module so
    # tests can patch it. Later quota code reads it from the result.
    tier = await entitlements.tier_for(db, user)

    provider = _build_provider(spec)
    candidates = await provider.identify(payload.image_bytes)
    return AIResult(
        feature=spec.name,
        provider=spec.provider,
        model=spec.model,
        candidates=candidates,
        tier=tier,
    )

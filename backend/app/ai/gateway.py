"""The single entry point for paid AI calls.

Callers name a feature; the registry picks provider, model, token cap and
timeout, and ``ai_settings`` (``app.ai.settings``) may switch the feature off
or override its provider/model within the registry's allow-list. Nothing from
the client reaches those settings.
"""

from dataclasses import dataclass

import anyio
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import entitlements
from app.ai import settings as ai_settings
from app.ai.entitlements import Tier
from app.ai.providers.anthropic_vision import PROMPT_VERSION as ANTHROPIC_PROMPT
from app.ai.providers.anthropic_vision import AnthropicVisionProvider
from app.ai.providers.base import (
    ProviderOutcome,
    ProviderTimeoutError,
    VisionProvider,
)
from app.ai.providers.openai_vision import PROMPT_VERSION as OPENAI_PROMPT
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
    """What a feature call returns, with the provider's billed usage (#530).

    The token counts and ``outcome`` are the provider's
    (:class:`~app.ai.providers.base.ProviderResult`); the caller records them
    in the usage ledger (``app.ai.ledger``), which prices them.
    """

    feature: str
    provider: str
    model: str
    candidates: list[BookCandidate]
    tier: Tier
    outcome: ProviderOutcome = "ok"
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


def _build_provider(spec: FeatureSpec) -> VisionProvider:
    # Looked up at call time (not cached in a dict) so tests can patch the
    # provider class at this module's boundary.
    if spec.provider == "openai":
        return OpenAIVisionProvider(spec)
    if spec.provider == "anthropic":
        return AnthropicVisionProvider(spec)
    # Unreachable while the registry validates providers; fail closed anyway.
    raise ValueError(f"no provider adapter for {spec.provider!r}")


def resolve_spec(feature: str, current: ai_settings.AISettings) -> FeatureSpec:
    """The spec a call of ``feature`` uses under the ``current`` settings: the
    registry default with the validated provider/model override and kill
    switch applied. ``/scan`` uses it to name the model in the usage ledger
    when the call raised and no :class:`AIResult` exists."""
    return current.spec_for(get_feature(feature))


# Each adapter's prompt version (next to its system prompt), part of the
# result-cache key (#534). Exposed here so nothing outside the gateway
# imports the providers.
_PROMPT_VERSIONS = {"openai": OPENAI_PROMPT, "anthropic": ANTHROPIC_PROMPT}


def prompt_version(spec: FeatureSpec) -> str:
    """The prompt version of the adapter ``spec`` uses (its provider's
    ``PROMPT_VERSION``). Raises ``ValueError`` for a provider with no
    adapter, like :func:`_build_provider`."""
    try:
        return _PROMPT_VERSIONS[spec.provider]
    except KeyError:
        raise ValueError(f"no provider adapter for {spec.provider!r}") from None


async def run_feature(
    feature: str,
    *,
    user: User,
    db: AsyncSession,
    payload: VisionPayload,
    settings_snapshot: ai_settings.AISettings | None = None,
) -> AIResult:
    """Run one AI feature and return its result.

    ``user`` and ``db`` are accepted so later stories (per-user limits) can
    use them without changing callers. The usage ledger row is written by the
    caller from the returned :class:`AIResult` (``/scan``, #530).

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

    The whole provider call is bounded by the spec's ``timeout_s`` (an
    overall deadline, on top of the provider client's own per-phase HTTP
    timeouts). Past it the call is cancelled and ``ProviderTimeoutError`` (a
    ``ScanUnavailableError``) is raised, so callers treat it like any other unavailability (``/scan``
    refunds the reservation), and the quota lease (``timeout_s`` + 30 s)
    always outlives the call. It lives here, not in a provider, so every
    adapter gets it.
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
    # move_on_after rather than fail_after, so only this deadline (not a
    # TimeoutError raised inside the provider) becomes ScanUnavailableError.
    with anyio.move_on_after(spec.timeout_s) as deadline:
        provided = await provider.identify(payload.image_bytes)
    if deadline.cancelled_caught:
        raise ProviderTimeoutError(
            f"AI feature {spec.name!r} exceeded its {spec.timeout_s}s deadline"
        )
    return AIResult(
        feature=spec.name,
        provider=spec.provider,
        model=spec.model,
        candidates=provided.candidates,
        tier=tier,
        outcome=provided.outcome,
        input_tokens=provided.input_tokens,
        output_tokens=provided.output_tokens,
        cache_read_tokens=provided.cache_read_tokens,
        cache_write_tokens=provided.cache_write_tokens,
    )

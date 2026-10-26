"""AI gateway routing — provider mocked at the provider boundary, no HTTP."""

import dataclasses
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import httpx
import pytest

from app.ai import gateway
from app.ai.entitlements import Tier
from app.ai.gateway import (
    AIResult,
    FeatureDisabledError,
    VisionPayload,
    run_feature,
)
from app.ai.providers.base import ProviderResult
from app.ai.providers.openai_vision import OpenAIVisionProvider
from app.ai.registry import FEATURES, UnknownFeatureError
from app.services.book_identifier import BookCandidate, ScanUnavailableError

USER = MagicMock(name="user")
DB = MagicMock(name="db")
PAYLOAD = VisionPayload(image_bytes=b"img")
CANDIDATE = BookCandidate(title="Dune", author="Frank Herbert", confidence=0.9)


@pytest.fixture
def provider_cls():
    with patch("app.ai.gateway.OpenAIVisionProvider") as cls:
        cls.return_value.identify = AsyncMock(return_value=ProviderResult([CANDIDATE]))
        yield cls


@pytest.mark.parametrize(
    ("feature", "model"),
    [("scan.standard", "gpt-4o-mini"), ("scan.enhanced", "gpt-4o")],
)
async def test_feature_routes_to_registry_model(provider_cls, feature, model):
    result = await run_feature(feature, user=USER, db=DB, payload=PAYLOAD)

    provider_cls.assert_called_once_with(FEATURES[feature])
    assert provider_cls.call_args.args[0].model == model
    provider_cls.return_value.identify.assert_awaited_once_with(b"img")
    assert result == AIResult(
        feature=feature,
        provider="openai",
        model=model,
        candidates=[CANDIDATE],
        tier=Tier.FREE,
    )


async def test_real_provider_receives_spec_model_and_limits():
    """The adapter sends the registry's model, max_tokens and timeout."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"choices": [{"message": {"content": '{"books": []}'}}]}
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    spec = FEATURES["scan.enhanced"]
    with patch(
        "app.ai.providers.openai_vision.httpx.AsyncClient", return_value=client
    ) as client_cls:
        result = await run_feature("scan.enhanced", user=USER, db=DB, payload=PAYLOAD)

    assert result.candidates == []
    client_cls.assert_called_once_with(timeout=spec.timeout_s)
    body = client.post.call_args.kwargs["json"]
    assert body["model"] == spec.model
    assert body["max_tokens"] == spec.max_tokens


@pytest.mark.parametrize("name", ["scan.premium", "", "scan.standard "])
async def test_unknown_feature_rejected_before_provider(provider_cls, name):
    with pytest.raises(UnknownFeatureError):
        await run_feature(name, user=USER, db=DB, payload=PAYLOAD)
    provider_cls.assert_not_called()


async def test_disabled_feature_never_reaches_provider(provider_cls):
    disabled = dataclasses.replace(FEATURES["scan.standard"], enabled=False)
    # FEATURES is read-only, so patch the lookup instead.
    with (
        patch("app.ai.gateway.get_feature", return_value=disabled),
        pytest.raises(FeatureDisabledError) as exc_info,
    ):
        await run_feature("scan.standard", user=USER, db=DB, payload=PAYLOAD)
    # Subclass of ScanUnavailableError, so /scan refunds and returns 503.
    assert isinstance(exc_info.value, ScanUnavailableError)
    provider_cls.assert_not_called()


async def test_wrong_payload_type_rejected(provider_cls):
    with pytest.raises(TypeError):
        await run_feature(
            "scan.standard", user=USER, db=DB, payload=b"raw-bytes"  # type: ignore[arg-type]
        )
    provider_cls.assert_not_called()


@pytest.mark.parametrize(
    "error", [ScanUnavailableError("down"), httpx.ConnectError("refused")]
)
async def test_provider_errors_propagate(provider_cls, error):
    provider_cls.return_value.identify = AsyncMock(side_effect=error)
    with pytest.raises(type(error)):
        await run_feature("scan.standard", user=USER, db=DB, payload=PAYLOAD)


def test_unknown_provider_has_no_adapter():
    spec = MagicMock(provider="acme")
    with pytest.raises(ValueError):
        gateway._build_provider(spec)


def test_openai_adapter_rejects_non_openai_spec():
    spec = MagicMock(provider="acme", name="x")
    with pytest.raises(ValueError):
        OpenAIVisionProvider(spec)


def _fast(spec, timeout_s: float):
    """A copy of ``spec`` with a timeout below the registry's 1 s floor, so a
    test need not wait the real 45 s (bypasses the frozen dataclass)."""
    fast = dataclasses.replace(spec)
    object.__setattr__(fast, "timeout_s", timeout_s)
    return fast


class TestOverallDeadline:
    """The gateway bounds the whole provider call by the feature's timeout_s
    (#528), so the lease's timeout_s + 30 s really outlives the call."""

    async def test_provider_slower_than_timeout_raises_scan_unavailable(
        self, provider_cls
    ):
        started = []

        async def slow(_image):
            started.append(True)
            await anyio.sleep(30)
            return ProviderResult([CANDIDATE])

        provider_cls.return_value.identify = slow
        snapshot = MagicMock(spec_for=lambda s: _fast(s, 0.05))
        with anyio.fail_after(5):  # the test itself never waits long
            with pytest.raises(ScanUnavailableError) as exc_info:
                await run_feature(
                    "scan.standard",
                    user=USER,
                    db=DB,
                    payload=PAYLOAD,
                    settings_snapshot=snapshot,
                )
        assert started == [True]
        assert "deadline" in str(exc_info.value)
        # Not a kill switch: a plain unavailability, which /scan refunds.
        assert not isinstance(exc_info.value, FeatureDisabledError)

    async def test_provider_within_timeout_returns(self, provider_cls):
        async def quick(_image):
            await anyio.sleep(0.01)
            return ProviderResult([CANDIDATE])

        provider_cls.return_value.identify = quick
        snapshot = MagicMock(spec_for=lambda s: _fast(s, 1.0))
        result = await run_feature(
            "scan.standard",
            user=USER,
            db=DB,
            payload=PAYLOAD,
            settings_snapshot=snapshot,
        )
        assert result.candidates == [CANDIDATE]

    async def test_deadline_is_the_specs_timeout(self, provider_cls):
        seen = []
        real_move_on_after = gateway.anyio.move_on_after

        def spy(delay, *args, **kwargs):
            seen.append(delay)
            return real_move_on_after(delay, *args, **kwargs)

        with patch.object(gateway.anyio, "move_on_after", spy):
            await run_feature("scan.enhanced", user=USER, db=DB, payload=PAYLOAD)
        assert seen == [FEATURES["scan.enhanced"].timeout_s]

    async def test_providers_own_timeout_error_is_not_the_deadline(self, provider_cls):
        """Only the gateway's deadline maps to ScanUnavailableError (and a
        refund); a TimeoutError raised by the provider itself propagates."""

        async def raises(_image):
            raise TimeoutError("from inside the provider")

        provider_cls.return_value.identify = raises
        snapshot = MagicMock(spec_for=lambda s: _fast(s, 5.0))
        with pytest.raises(TimeoutError, match="inside the provider"):
            await run_feature(
                "scan.standard",
                user=USER,
                db=DB,
                payload=PAYLOAD,
                settings_snapshot=snapshot,
            )

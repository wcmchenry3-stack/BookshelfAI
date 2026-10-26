"""Cost guard (#584): nothing under pytest calls a real AI provider.

- The guard (``tests/_network_guard.py``, installed for the whole run by
  ``tests/conftest.py``) blocks a request to the Anthropic or OpenAI API from
  the real adapters, sync or async, before anything is sent.
- The limits matrix's provider is the fake: inside ``FakeProvider.patch()``
  the gateway builds no real adapter, SDK client or HTTP client, for either
  feature on either provider.

No database needed: these run in every backend test job.
"""

from dataclasses import replace
from unittest.mock import MagicMock, patch

import httpx
import httpx2
import pytest

from app.ai import gateway
from app.ai import settings as ai_settings
from app.ai.providers.anthropic_vision import AnthropicVisionProvider
from app.ai.providers.openai_vision import OpenAIVisionProvider
from app.ai.registry import get_feature
from app.core.config import settings
from tests import _network_guard as guard
from tests.limits import _harness as h


@pytest.fixture
def expect_blocked():
    """For the tests that trip the guard on purpose: check one attempt was
    recorded, then clear it so the conftest check passes."""
    yield
    assert guard.attempts, "the guard did not see the request"
    guard.attempts.clear()


@pytest.mark.parametrize(
    "host, blocked",
    [
        ("api.anthropic.com", True),
        ("API.OpenAI.com.", True),
        ("eu.api.anthropic.com", True),
        ("api.openai.com", True),
        ("openlibrary.org", False),
        ("www.googleapis.com", False),
        ("challenges.cloudflare.com", False),
        ("notanthropic.com", False),
    ],
)
def test_blocked_hosts(host, blocked):
    assert guard.is_blocked(host) is blocked


async def test_the_real_openai_adapter_is_blocked(expect_blocked):
    provider = OpenAIVisionProvider(get_feature("scan.standard"))
    with (
        patch.object(settings, "openai_api_key", "sk-test-not-real"),
        pytest.raises(guard.RealProviderCallBlocked),
    ):
        await provider.identify(h._JPEG)
    assert guard.attempts == ["POST api.openai.com"]


async def test_the_real_anthropic_adapter_is_blocked(expect_blocked):
    spec = replace(
        get_feature("scan.standard"), provider="anthropic", model="claude-haiku-5-5"
    )
    provider = AnthropicVisionProvider(spec)
    with (
        patch.object(settings, "anthropic_api_key", "sk-ant-test-not-real"),
        pytest.raises(guard.RealProviderCallBlocked),
    ):
        await provider.identify(h._JPEG)
    # Blocked once: the SDK and adapter retry loops cannot catch it.
    assert guard.attempts == ["POST api.anthropic.com"]


@pytest.mark.parametrize("module", [httpx, httpx2], ids=["httpx", "httpx2"])
def test_plain_sync_clients_are_blocked(module, expect_blocked):
    with pytest.raises(guard.RealProviderCallBlocked):
        module.post("https://api.anthropic.com/v1/messages", json={})


def test_the_guard_reports_an_attempt_that_was_swallowed():
    """The conftest check fails a test whose blocked call was swallowed: the
    attempt is still recorded."""
    swallowed = []
    with httpx.Client() as client:
        try:
            client.get("https://api.openai.com/v1/models")
        except BaseException as exc:  # noqa: BLE001 - simulating a swallow
            swallowed.append(type(exc))
    assert swallowed == [guard.RealProviderCallBlocked]
    assert guard.attempts == ["GET api.openai.com"]
    guard.attempts.clear()


@pytest.mark.parametrize("feature", ["scan.standard", "scan.enhanced"])
@pytest.mark.parametrize("provider_name", ["openai", "anthropic"])
async def test_the_matrix_provider_is_the_fake(feature, provider_name):
    """With the matrix's fake in place the gateway builds no real adapter,
    SDK client or HTTP client, whichever provider ai_settings selects."""
    current = ai_settings.AISettings({})
    if provider_name == "anthropic":
        model = {
            "scan.standard": "claude-haiku-5-5",
            "scan.enhanced": "claude-sonnet-5-5",
        }
        current = ai_settings.AISettings(
            {f"{feature}.provider": "anthropic", f"{feature}.model": model[feature]}
        )
    assert gateway.resolve_spec(feature, current).provider == provider_name

    no_client = MagicMock(side_effect=AssertionError("a real client was built"))
    fake = h.FakeProvider("books")
    with (
        fake.patch(),
        patch("app.ai.providers.anthropic_vision.anthropic.AsyncAnthropic", no_client),
        patch("app.ai.providers.openai_vision.httpx.AsyncClient", no_client),
    ):
        assert gateway.OpenAIVisionProvider is fake.cls
        assert gateway.AnthropicVisionProvider is fake.cls
        result = await gateway.run_feature(
            feature,
            user=MagicMock(),
            db=MagicMock(),
            payload=gateway.VisionPayload(image_bytes=h._JPEG),
            settings_snapshot=current,
        )
    assert [c.title for c in result.candidates] == ["Dune"]
    assert fake.calls == 1 and fake.built == 1
    assert fake.cls.call_args.args[0].provider == provider_name
    no_client.assert_not_called()

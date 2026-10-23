"""The Claude adapter and AI_ANTHROPIC_BASE_URL (#539): the client is built with
the pinned Anthropic host unless the validated setting names another."""

import dataclasses
from unittest.mock import patch

import pytest

from app.ai.providers.anthropic_vision import (
    ANTHROPIC_BASE_URL,
    AnthropicVisionProvider,
)
from app.ai.registry import FEATURES, default_model
from app.core.config import settings
from tests._claude_fake import fake_claude, message

IMAGE = b"\xff\xd8\xff-fake-jpeg"


@pytest.fixture
def provider():
    spec = dataclasses.replace(
        FEATURES["scan.standard"],
        provider="anthropic",
        model=default_model("scan.standard", "anthropic"),
    )
    return AnthropicVisionProvider(spec)


async def test_unset_uses_the_pinned_host(provider):
    with (
        patch.object(settings, "ai_anthropic_base_url", ""),
        fake_claude(message()) as (client_cls, _create),
    ):
        await provider.identify(IMAGE)
    assert client_cls.call_args.kwargs["base_url"] == ANTHROPIC_BASE_URL
    assert ANTHROPIC_BASE_URL == "https://api.anthropic.com"


async def test_set_is_passed_to_the_client(provider):
    with (
        patch.object(settings, "ai_anthropic_base_url", "http://127.0.0.1:18080"),
        fake_claude(message()) as (client_cls, _create),
    ):
        await provider.identify(IMAGE)
    assert client_cls.call_args.kwargs["base_url"] == "http://127.0.0.1:18080"


async def test_the_generic_anthropic_base_url_is_ignored(provider, monkeypatch):
    # A developer's shell may export ANTHROPIC_BASE_URL for other tools. Neither
    # the app's settings nor the SDK may use it, or the API key would go there.
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example.com")
    from app.core.config import Settings

    assert (
        Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db"
        ).ai_anthropic_base_url
        == ""
    )
    with (
        patch.object(settings, "ai_anthropic_base_url", ""),
        fake_claude(message()) as (client_cls, _create),
    ):
        await provider.identify(IMAGE)
    assert client_cls.call_args.kwargs["base_url"] == "https://api.anthropic.com"

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


async def test_override_client_does_not_follow_redirects(provider):
    # A local endpoint answering 307/308 to another host would otherwise carry
    # the X-Api-Key header and the photo there (httpx only strips Authorization).
    with (
        patch.object(settings, "ai_anthropic_base_url", "http://127.0.0.1:18080"),
        fake_claude(message()) as (client_cls, _create),
        patch(
            "app.ai.providers.anthropic_vision.anthropic.DefaultAsyncHttpxClient"
        ) as http_client,
    ):
        await provider.identify(IMAGE)
    http_client.assert_called_once_with(follow_redirects=False)
    kwargs = client_cls.call_args.kwargs
    assert kwargs["http_client"] is http_client.return_value
    assert kwargs["max_retries"] == 0
    assert kwargs["timeout"] == provider.spec.timeout_s


async def test_default_client_is_unchanged_when_unset(provider):
    with (
        patch.object(settings, "ai_anthropic_base_url", ""),
        fake_claude(message()) as (client_cls, _create),
    ):
        await provider.identify(IMAGE)
    assert "http_client" not in client_cls.call_args.kwargs


async def test_a_redirect_from_the_override_is_not_followed(provider):
    """Real SDK client, real HTTP: the local endpoint answers 307 to a second
    local server, which must never be reached."""
    import http.server
    import threading

    hits: list[str] = []

    class Target(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(self.headers.get("x-api-key", ""))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    target = http.server.HTTPServer(("127.0.0.1", 0), Target)
    target_url = f"http://127.0.0.1:{target.server_address[1]}/v1/messages"

    class Redirector(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(307)
            self.send_header("Location", target_url)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    redirector = http.server.HTTPServer(("127.0.0.1", 0), Redirector)
    for server in (target, redirector):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with (
            patch.object(
                settings,
                "ai_anthropic_base_url",
                f"http://127.0.0.1:{redirector.server_address[1]}",
            ),
            patch.object(settings, "anthropic_api_key", "secret-key"),
            pytest.raises(Exception),  # noqa: B017 - ScanUnavailableError
        ):
            await provider.identify(IMAGE)
    finally:
        for server in (target, redirector):
            server.shutdown()
            server.server_close()
    assert hits == []

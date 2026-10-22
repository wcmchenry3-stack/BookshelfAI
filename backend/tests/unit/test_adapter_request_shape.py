"""Each adapter sends exactly what its pure ``build_request`` returns (#534).

The result-cache key includes ``gateway.request_fingerprint``, which hashes
``build_request(spec, b"")``. If an adapter added anything to the request
after ``build_request`` (a parameter, a header in the body, a changed
prompt), the fingerprint would no longer describe what is sent and a cached
result could outlive a request change. These tests pin the outgoing request
to ``build_request`` exactly. Providers are mocked; no real model is called.
"""

import dataclasses
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai.providers import anthropic_vision, openai_vision
from app.ai.registry import FEATURES, default_model
from app.core.config import settings
from tests._claude_fake import fake_claude, message

IMAGE = b"\xff\xd8\xff-fake-jpeg"


def _openai_client(body):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = body
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@pytest.mark.parametrize("feature", ["scan.standard", "scan.enhanced"])
async def test_openai_sends_exactly_build_request(feature):
    spec = FEATURES[feature]
    body = {
        "choices": [
            {"message": {"content": json.dumps({"books": []})}, "finish_reason": "stop"}
        ]
    }
    client = _openai_client(body)
    with (
        patch("app.ai.providers.openai_vision.httpx.AsyncClient", return_value=client),
        patch.object(settings, "openai_api_key", "test-key"),
    ):
        await openai_vision.OpenAIVisionProvider(spec).identify(IMAGE)

    (call,) = client.post.await_args_list
    assert call.args == (openai_vision.OPENAI_URL,)
    # json is the whole request body the model sees.
    assert call.kwargs["json"] == openai_vision.build_request(
        spec, IMAGE, settings.scan_max_books
    )
    # The only other argument is the auth header: transport, not request
    # content, so it is deliberately not part of the fingerprint.
    assert set(call.kwargs) == {"json", "headers"}
    assert call.kwargs["headers"] == {"Authorization": "Bearer test-key"}


@pytest.mark.parametrize("feature", ["scan.standard", "scan.enhanced"])
async def test_claude_sends_exactly_build_request(feature):
    spec = dataclasses.replace(
        FEATURES[feature],
        provider="anthropic",
        model=default_model(feature, "anthropic"),
    )
    with fake_claude(message(books=[])) as (_client_cls, create):
        await anthropic_vision.AnthropicVisionProvider(spec).identify(IMAGE)

    (call,) = create.await_args_list
    assert call.args == ()
    # Every keyword is request content; no transport-level kwargs are added
    # per call (api key, host, timeout and retries are set on the client).
    assert call.kwargs == anthropic_vision.build_request(spec, IMAGE)

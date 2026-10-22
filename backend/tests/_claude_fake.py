"""A fake Anthropic SDK client for tests (#532): no network, no real model.

``fake_claude(...)`` patches ``AsyncAnthropic`` where the Claude adapter looks
it up and returns the fake client, whose ``messages.create`` is an
``AsyncMock`` (assert on its ``call_args`` for the request shape). Replies are
real ``anthropic.types.Message`` objects, built with :func:`message`.
"""

import json
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import httpx2

API_URL = "https://api.anthropic.com/v1/messages"


def message(
    text: str | None = None,
    *,
    books: list[dict] | None = None,
    stop_reason: str = "end_turn",
    model: str = "claude-haiku-5-5",
    input_tokens: int = 1_200,
    output_tokens: int = 300,
    cache_read: int | None = 0,
    cache_write: int | None = 0,
    thinking: bool = False,
) -> anthropic.types.Message:
    """A Messages API reply. ``books`` is shorthand for ``{"books": books}``."""
    if text is None:
        text = json.dumps({"books": books or []})
    content: list[dict[str, Any]] = []
    if thinking:
        content.append({"type": "thinking", "thinking": "", "signature": "sig"})
    if text:
        content.append({"type": "text", "text": text})
    return anthropic.types.Message.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_write,
            },
        }
    )


def status_error(
    status: int, retry_after: str | None = None
) -> anthropic.APIStatusError:
    """The SDK's exception for an HTTP ``status`` reply."""
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    response = httpx2.Response(
        status,
        headers=headers,
        request=httpx2.Request("POST", API_URL),
        json={"type": "error", "error": {"type": "api_error", "message": "x"}},
    )
    classes = {
        400: anthropic.BadRequestError,
        401: anthropic.AuthenticationError,
        404: anthropic.NotFoundError,
        429: anthropic.RateLimitError,
        503: anthropic.ServiceUnavailableError,
        529: anthropic.OverloadedError,
    }
    cls = classes.get(status)
    if cls is None:
        cls = (
            anthropic.InternalServerError if status >= 500 else anthropic.APIStatusError
        )
    return cls("error", response=response, body=None)


def timeout_error() -> anthropic.APITimeoutError:
    return anthropic.APITimeoutError(request=httpx2.Request("POST", API_URL))


def connect_error() -> anthropic.APIConnectionError:
    """Never connected: the SDK wraps httpx2.ConnectError."""
    exc = anthropic.APIConnectionError(request=httpx2.Request("POST", API_URL))
    exc.__cause__ = httpx2.ConnectError("refused")
    return exc


def dropped_error() -> anthropic.APIConnectionError:
    """Connected, then the connection dropped mid-request."""
    exc = anthropic.APIConnectionError(request=httpx2.Request("POST", API_URL))
    exc.__cause__ = httpx2.ReadError("reset")
    return exc


@contextmanager
def fake_claude(*replies: Any, api_key: str = "test-anthropic-key"):
    """Patch the SDK client class; each ``messages.create`` call returns (or
    raises) the next of ``replies``. Yields ``(client_cls, create)``."""
    client = MagicMock(name="AsyncAnthropic()")
    create = AsyncMock(side_effect=list(replies))
    client.messages.create = create
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    with (
        patch(
            "app.ai.providers.anthropic_vision.anthropic.AsyncAnthropic",
            return_value=client,
        ) as client_cls,
        patch("app.ai.providers.anthropic_vision.settings.anthropic_api_key", api_key),
    ):
        yield client_cls, create

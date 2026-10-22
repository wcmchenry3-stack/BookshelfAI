"""Cost guard (#584): no test may send a request to a real AI provider.

``install()`` wraps the request method of the default HTTP transports of
``httpx`` (the OpenAI adapter's client) and ``httpx2`` (the Anthropic SDK's
client), sync and async. A request to an Anthropic or OpenAI API host raises
:class:`RealProviderCallBlocked` before anything is sent, and is recorded in
:data:`attempts` so ``tests/conftest.py`` fails the test even if something
swallowed the exception. Requests to any other host, and clients built on a
mock transport, are untouched.

Tests fake providers at the gateway boundary (``tests/limits/_harness.py``,
``tests/_claude_fake.py``) or patch the client class; this guard is the
backstop if one forgets.
"""

from collections.abc import Callable
from contextlib import ExitStack
from unittest.mock import patch

import httpx
import httpx2

BLOCKED_HOSTS = frozenset({"api.anthropic.com", "api.openai.com"})
BLOCKED_DOMAINS = ("anthropic.com", "openai.com")

# Every blocked request, as "METHOD host"; reset by the conftest fixture.
attempts: list[str] = []


class RealProviderCallBlocked(BaseException):
    """A test tried to reach a real AI provider. A BaseException so that no
    ``except Exception`` in an adapter or SDK retry loop can swallow it."""


def is_blocked(host: str) -> bool:
    host = host.lower().rstrip(".")
    return host in BLOCKED_HOSTS or any(
        host == d or host.endswith("." + d) for d in BLOCKED_DOMAINS
    )


def _check(request) -> None:
    host = request.url.host
    if is_blocked(host):
        attempts.append(f"{request.method} {host}")
        raise RealProviderCallBlocked(
            f"a test tried to call a real AI provider ({request.method} {host}); "
            "fake it at the gateway boundary (tests/limits/_harness.py) or "
            "with tests/_claude_fake.py"
        )


def _guard_sync(original: Callable) -> Callable:
    def handle_request(self, request):
        _check(request)
        return original(self, request)

    return handle_request


def _guard_async(original: Callable) -> Callable:
    async def handle_async_request(self, request):
        _check(request)
        return await original(self, request)

    return handle_async_request


def install() -> ExitStack:
    """Start the guard; close the returned stack to remove it."""
    stack = ExitStack()
    for module in (httpx, httpx2):
        stack.enter_context(
            patch.object(
                module.HTTPTransport,
                "handle_request",
                _guard_sync(module.HTTPTransport.handle_request),
            )
        )
        stack.enter_context(
            patch.object(
                module.AsyncHTTPTransport,
                "handle_async_request",
                _guard_async(module.AsyncHTTPTransport.handle_async_request),
            )
        )
    return stack

"""Sentry context enrichment — attaches request-level tags and user identity."""

import sentry_sdk
from fastapi import Request
from sentry_sdk.scrubber import DEFAULT_DENYLIST, EventScrubber
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.core.logging import request_id_var


class SentryContextMiddleware(BaseHTTPMiddleware):
    """Attach request_id and endpoint tags to every Sentry event.

    User identity is set separately via ``set_sentry_user`` inside route
    handlers (after FastAPI dependency injection resolves the current user).
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        sentry_sdk.set_tag("request_id", request_id_var.get())
        sentry_sdk.set_tag("endpoint", request.url.path)
        sentry_sdk.set_tag("method", request.method)
        return await call_next(request)


def set_sentry_user(user_id: int | str) -> None:
    """Set Sentry user context with ID only (no PII)."""
    sentry_sdk.set_user({"id": str(user_id)})


# sentry-sdk's default scrubber matches key names EXACTLY ("token", "secret",
# ...), so this API's real credential fields slip straight past it. Name them.
_APP_DENYLIST = [
    "id_token",  # Google ID token posted to /auth/google
    "access_token",
    "refresh_token",
    "credential",
    "cf-turnstile-response",
    "turnstile_token",
    "test_auth_secret",
    "jwt_private_key",
    "openai_api_key",
    "database_url",
]


def build_event_scrubber() -> EventScrubber:
    """Scrubber for every outgoing Sentry event.

    ``recursive=True`` so credentials nested inside JSON request bodies and
    ``extra`` dicts are filtered too, not just top-level keys.
    """
    return EventScrubber(denylist=[*DEFAULT_DENYLIST, *_APP_DENYLIST], recursive=True)

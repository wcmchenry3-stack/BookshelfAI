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


# --- What may leave this process for Sentry ---------------------------------
#
# sentry-sdk's scrubber only matches dict KEY NAMES, exactly. That cannot see a
# token inside a Pydantic model's repr, a third-party library's local, or a URL
# query string — so the key-name list below is only one of three layers:
#
#   1. include_local_variables=False — frame locals are never sent. Nearly every
#      auth frame holds an ID/refresh token (often inside a model repr), and no
#      name list can keep up with that.
#   2. before_* hooks — strip URL query strings (the Google Books API key travels
#      as ``?key=``) from spans and breadcrumbs.
#   3. EventScrubber — request bodies, headers, extras and contexts, by key name.

_SECRET_FIELD_MARKERS = ("secret", "key", "token", "password", "dsn", "database_url")

# Request/handler names that are not Settings fields.
_REQUEST_DENYLIST = [
    "id_token",  # Google ID token posted to /auth/google
    "access_token",
    "refresh_token",
    "new_refresh",
    "credential",
    "cf-turnstile-response",  # header / form field
    "cf_turnstile_response",  # handler parameter name
    "provided_secret",
    "http.query",
]


def _settings_secret_names() -> list[str]:
    """Secret-looking Settings field names, derived so the list cannot drift."""
    from app.core.config import Settings

    return [
        name
        for name in Settings.model_fields
        if any(marker in name for marker in _SECRET_FIELD_MARKERS)
    ]


def build_event_scrubber() -> EventScrubber:
    """Key-name scrubber; ``recursive=True`` reaches into nested bodies/extras."""
    return EventScrubber(
        denylist=[*DEFAULT_DENYLIST, *_REQUEST_DENYLIST, *_settings_secret_names()],
        recursive=True,
    )


_FILTERED = "[Filtered]"


def _strip_query(data: dict) -> None:
    """Drop URL query strings from a span/breadcrumb ``data`` dict, in place."""
    if "http.query" in data:
        data["http.query"] = _FILTERED
    for key in ("url", "http.url"):
        value = data.get(key)
        if isinstance(value, str) and "?" in value:
            data[key] = value.split("?", 1)[0]


def _scrub_breadcrumb(crumb: dict, _hint: dict | None = None) -> dict:
    if isinstance(crumb.get("data"), dict):
        _strip_query(crumb["data"])
    return crumb


def _scrub_event(event: dict, _hint: dict | None = None) -> dict:
    """before_send / before_send_transaction: query strings never leave."""
    for span in event.get("spans") or []:
        if isinstance(span.get("data"), dict):
            _strip_query(span["data"])
        description = span.get("description")
        if isinstance(description, str) and "?" in description:
            span["description"] = description.split("?", 1)[0]
    crumbs = event.get("breadcrumbs")
    values = crumbs.get("values") if isinstance(crumbs, dict) else crumbs
    for crumb in values or []:
        _scrub_breadcrumb(crumb)
    request = event.get("request")
    if isinstance(request, dict) and request.get("query_string"):
        request["query_string"] = _FILTERED
    return event


def sentry_privacy_options() -> dict:
    """Privacy-relevant ``sentry_sdk.init`` options, in one testable place.

    ``send_default_pii=False`` is passed alongside these in ``app/main.py``.
    """
    return {
        "include_local_variables": False,
        "event_scrubber": build_event_scrubber(),
        "before_send": _scrub_event,
        "before_send_transaction": _scrub_event,
        "before_breadcrumb": _scrub_breadcrumb,
    }

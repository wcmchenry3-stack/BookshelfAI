"""Sentry context enrichment — attaches request-level tags and user identity."""

import sentry_sdk
from fastapi import Request
from sentry_sdk.scrubber import DEFAULT_DENYLIST, EventScrubber
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.core.client_ip import CF_CONNECTING_IP
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
    "catalog_token",  # POST /wishlist body and EnrichedBook (#602)
]


def _settings_secret_names() -> list[str]:
    """Secret-looking Settings field names, derived so the list cannot drift."""
    from app.core.config import Settings

    return [
        name
        for name in Settings.model_fields
        if any(marker in name for marker in _SECRET_FIELD_MARKERS)
    ]


def _configured_header_names() -> list[str]:
    """Request headers named by settings that carry a secret value.

    The origin-lockdown header (``ORIGIN_AUTH_HEADER``) holds the shared
    origin secret on every request; its name is configurable, so it is read
    from settings rather than hard-coded.
    """
    from app.core.config import settings

    return [settings.origin_auth_header.lower()]


def build_event_scrubber() -> EventScrubber:
    """Key-name scrubber; ``recursive=True`` reaches into nested bodies/extras."""
    return EventScrubber(
        denylist=[
            *DEFAULT_DENYLIST,
            *_REQUEST_DENYLIST,
            *_settings_secret_names(),
            *_configured_header_names(),
        ],
        recursive=True,
    )


_FILTERED = "[Filtered]"

# Headers that identify where the caller is. Removed (not masked) from every
# event, case-insensitively. ``sentry-sdk`` only filters ``X-Forwarded-For`` and
# ``X-Real-IP`` by default, so Cloudflare's ``CF-Connecting-IP`` — the header the
# API itself trusts for the client IP — reached Sentry in the clear (#565).
# ``CF-IPCountry`` is approximate location. ``CF-Ray`` is deliberately kept.
_CLIENT_IP_HEADERS = frozenset(
    {
        CF_CONNECTING_IP,
        "cf-connecting-ipv6",
        "cf-pseudo-ipv4",
        "cf-ipcountry",
        "true-client-ip",
        "x-client-ip",
        "x-forwarded-for",
        "x-real-ip",
        "forwarded",
    }
)
# WSGI/ASGI-style ``request.env`` keys that carry the peer address.
_CLIENT_IP_ENV_KEYS = frozenset({"remote_addr", "remote_host", "http_x_forwarded_for"})


def _strip_client_ip(event: dict) -> None:
    """Remove client IP / location from ``request`` and ``user``, in place."""
    request = event.get("request")
    if isinstance(request, dict):
        headers = request.get("headers")
        if isinstance(headers, dict):
            for name in [k for k in headers if str(k).lower() in _CLIENT_IP_HEADERS]:
                del headers[name]
        elif isinstance(headers, list):
            request["headers"] = [
                h
                for h in headers
                if not (
                    isinstance(h, (list, tuple))
                    and h
                    and str(h[0]).lower() in _CLIENT_IP_HEADERS
                )
            ]
        env = request.get("env")
        if isinstance(env, dict):
            for name in [k for k in env if str(k).lower() in _CLIENT_IP_ENV_KEYS]:
                del env[name]
    user = event.get("user")
    if isinstance(user, dict):
        user.pop("ip_address", None)


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
    _strip_client_ip(event)
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

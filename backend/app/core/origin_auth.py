"""Origin lockdown: reject requests that did not come through the edge.

When ``settings.origin_auth_secret`` is set, every request must carry the
header named by ``settings.origin_auth_header`` with exactly that value; the
edge in front of the API adds it. Anything else gets ``403 {"detail":
"forbidden"}`` before routing, so no handler, database query or rate-limit
counter runs for it. ``/health`` is exempt so the platform's health check,
which calls the origin directly, keeps working.

An empty secret disables the check, so the code can be deployed before the
edge adds the header (rollout order: ``docs/security.md``).

The secret value never appears in responses or logs (the header name is not
echoed either), and the header is scrubbed from error reports by the Sentry
scrubber (``app.core.sentry_context.build_event_scrubber``).

A rejected request that carries ``Authorization: Bearer ...`` is almost
certainly our own app hitting a misconfigured lockdown (scanners send no
bearer tokens), so it is logged at WARNING and reported to Sentry as
``origin_auth_rejected_authenticated`` (level ``error``), at most once per
``ALERT_THROTTLE_SECONDS`` per process, with the number of such rejections
since the last report. Plain rejections stay at INFO. Allow/deny decisions are
unaffected. Neither the token, the origin header nor the secret is reported.
"""

from __future__ import annotations

import hmac
import logging
import time
from collections.abc import Callable

import sentry_sdk
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import settings

logger = logging.getLogger(__name__)

# Only the liveness probe: it never touches the database. /health/db is not
# exempt (its uptime monitor uses the public hostname, which goes through the
# edge).
ORIGIN_AUTH_EXEMPT_PATHS = frozenset({"/health"})

ALERT_MESSAGE = "origin_auth_rejected_authenticated"
ALERT_THROTTLE_SECONDS = 300.0
_MAX_TAG_PATH = 200

# Monotonic clock; tests replace it.
_clock: Callable[[], float] = time.monotonic
_last_alert_at: float | None = None
_rejected_since_alert = 0

_FORBIDDEN_BODY = b'{"detail":"forbidden"}'


class OriginAuthMiddleware:
    """Pure ASGI middleware enforcing the shared origin header."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _is_allowed(scope):
            await self.app(scope, receive, send)
            return

        if _has_bearer(scope):
            logger.warning(
                "origin_auth_rejected_authenticated method=%s path=%s",
                scope.get("method"),
                scope.get("path"),
            )
            _alert_authenticated_rejection(scope)
        else:
            logger.info(
                "origin_auth_rejected method=%s path=%s",
                scope.get("method"),
                scope.get("path"),
            )
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(_FORBIDDEN_BODY)).encode()),
                ],
            }
        )
        await send(
            {"type": "http.response.body", "body": _FORBIDDEN_BODY, "more_body": False}
        )


def _is_allowed(scope: Scope) -> bool:
    secret = settings.origin_auth_secret
    if not secret:
        return True
    if scope.get("path") in ORIGIN_AUTH_EXEMPT_PATHS:
        return True
    name = settings.origin_auth_header.lower().encode("latin-1")
    values = [v for k, v in scope.get("headers", []) if k.lower() == name]
    if len(values) != 1:
        return False
    return hmac.compare_digest(values[0], secret.encode("utf-8"))


def _has_bearer(scope: Scope) -> bool:
    for k, v in scope.get("headers", []):
        if k.lower() == b"authorization":
            return v[:7].lower() == b"bearer "
    return False


def _alert_authenticated_rejection(scope: Scope) -> None:
    """Send a throttled Sentry message; never raises into the request path."""
    global _last_alert_at, _rejected_since_alert
    _rejected_since_alert += 1
    now = _clock()
    if _last_alert_at is not None and now - _last_alert_at < ALERT_THROTTLE_SECONDS:
        return
    count = _rejected_since_alert
    _last_alert_at = now
    _rejected_since_alert = 0
    try:
        with sentry_sdk.isolation_scope() as sentry_scope:
            sentry_scope.set_tag("method", str(scope.get("method")))
            sentry_scope.set_tag("path", str(scope.get("path"))[:_MAX_TAG_PATH])
            sentry_scope.set_extra("rejected_since_last", count)
            sentry_sdk.capture_message(ALERT_MESSAGE, level="error")
    except Exception:  # pragma: no cover - alerting must not break the 403
        logger.exception("origin_auth_alert_failed")

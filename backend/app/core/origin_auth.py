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
"""

from __future__ import annotations

import hmac
import logging

from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import settings

logger = logging.getLogger(__name__)

# Only the liveness probe: it never touches the database. /health/db is not
# exempt (its uptime monitor uses the public hostname, which goes through the
# edge).
ORIGIN_AUTH_EXEMPT_PATHS = frozenset({"/health"})

_FORBIDDEN_BODY = b'{"detail":"forbidden"}'


class OriginAuthMiddleware:
    """Pure ASGI middleware enforcing the shared origin header."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _is_allowed(scope):
            await self.app(scope, receive, send)
            return

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
        await send({"type": "http.response.body", "body": _FORBIDDEN_BODY})


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

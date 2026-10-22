"""Service-level Sentry warnings that must carry nothing about the request.

Used by the cost breaker's spend alerts (#531, ``app/ai/breaker.py``) and the
zero-result enhanced-scan warning (#533, ``app/ai/quota.py``). The request's
scopes carry the user id (``set_sentry_user`` in ``get_current_user``), tags
and breadcrumbs; these warnings are about the service, not a user. So the
event is captured in forked, emptied isolation and current scopes (nothing of
the request leaks in, and nothing of the warning leaks into later events of
the same request), and an event processor strips what the Starlette/FastAPI
integration's own processor would still attach (the ``request`` block with
url, headers incl. client IP and body, ``user``, breadcrumbs, the route
``transaction`` name and any request context).

This module never calls a provider and must not import ``app.ai.providers``.
"""

from collections.abc import Mapping, Sequence
from typing import Any

import sentry_sdk

# SDK-generated contexts that say nothing about the request or the user.
KEPT_CONTEXTS = frozenset({"trace", "runtime", "os"})


def strip_request_data(event: dict, _hint: dict) -> dict:
    """Event processor: drop everything that came from the request (the HTTP
    ``request`` block, ``user``, breadcrumbs, the route ``transaction`` name,
    and any context other than the trace/runtime ones the SDK adds)."""
    for key in ("request", "user", "breadcrumbs", "transaction"):
        event.pop(key, None)
    contexts = event.get("contexts")
    if isinstance(contexts, dict):
        event["contexts"] = {k: v for k, v in contexts.items() if k in KEPT_CONTEXTS}
    return event


def capture_service_warning(
    message: str,
    *,
    fingerprint: Sequence[str],
    tags: Mapping[str, str],
    extras: Mapping[str, Any],
) -> None:
    """Capture one ``warning`` event with only ``tags``, ``extras`` and
    ``fingerprint``. Raises only what the SDK raises; callers that must never
    fail wrap it."""
    with sentry_sdk.isolation_scope() as scope:
        scope.clear()
        current = sentry_sdk.get_current_scope()
        current.clear()
        # clear() keeps the scopes' event processors, and the
        # Starlette/FastAPI integration's processor would still attach the
        # HTTP request. Added last, so it runs after the integration's.
        current.add_event_processor(strip_request_data)
        for key, value in tags.items():
            current.set_tag(key, value)
        for key, value in extras.items():
            current.set_extra(key, value)
        current.fingerprint = list(fingerprint)
        sentry_sdk.capture_message(message, level="warning")

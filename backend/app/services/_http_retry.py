"""Bounded retries for the outbound API clients (#694: object storage,
email).

:func:`send_with_retries` sends one request up to :data:`ATTEMPTS` times. A
transport error, a timeout, a 429 or a 5xx is retried after a backoff
(:data:`BACKOFF_S`, doubling); any other status is final. It logs the
operation label and the status or error type only: never the URL, headers
or body, which can hold keys, signed tokens or an email address.
"""

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

ATTEMPTS = 3  # first try + 2 retries
BACKOFF_S = 0.5  # 0.5 s, then 1 s
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class RequestFailed(Exception):
    """Every try failed. ``status`` is the last HTTP status, or ``None`` when
    the last try got no response (transport error, timeout). ``response`` is
    that last response, for a caller that reads a machine code from it;
    never log it.

    ``maybe_accepted`` is True when any try (not only the last) got no
    response or a 5xx: the server may have processed that request, whatever
    the later tries answered."""

    def __init__(
        self,
        status: int | None,
        response: httpx.Response | None = None,
        maybe_accepted: bool = False,
    ) -> None:
        super().__init__(f"request failed (status={status})")
        self.status = status
        self.response = response
        self.maybe_accepted = maybe_accepted


async def send_with_retries(
    client: httpx.AsyncClient, label: str, method: str, url: str, **kwargs: Any
) -> httpx.Response:
    """The first 2xx response, or :class:`RequestFailed`."""
    status: int | None = None
    last: httpx.Response | None = None
    maybe_accepted = False
    for attempt in range(1, ATTEMPTS + 1):
        try:
            resp = await client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            status, retry, reason, last = None, True, type(exc).__name__, None
            maybe_accepted = True
        else:
            last = resp
            if resp.is_success:
                return resp
            status = resp.status_code
            retry, reason = status in _RETRY_STATUSES, str(status)
            maybe_accepted = maybe_accepted or status >= 500
        if not retry or attempt == ATTEMPTS:
            break
        logger.warning("%s: retry %d/%d (%s)", label, attempt, ATTEMPTS - 1, reason)
        await asyncio.sleep(BACKOFF_S * 2 ** (attempt - 1))
    raise RequestFailed(status, last, maybe_accepted)

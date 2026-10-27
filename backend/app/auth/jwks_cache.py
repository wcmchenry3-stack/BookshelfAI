"""In-process JWKS cache shared by the identity-provider verifiers.

Behaviour:
- TTL comes from the response ``Cache-Control: max-age`` (fallback 1 h, capped
  at 24 h, floored at the refresh throttle).
- A token whose ``kid`` is not cached triggers one forced refresh, throttled to
  at most once per 60 s, before the caller is left to reject the token.
- Concurrent callers share a single in-flight fetch (asyncio.Lock).
- If a refresh fails but keys are cached, the cached keys keep being served
  until they are ``MAX_STALE_SECONDS`` (24 h) past expiry; after that the
  failure propagates.

Only public signing keys are stored. Nothing token-related is ever logged.
"""

import asyncio
import logging
import re
import time
from collections.abc import Callable

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 3600
MAX_TTL_SECONDS = 24 * 3600
MIN_REFRESH_INTERVAL_SECONDS = 60
# Hard limit: how long past expiry cached keys may still be served when the
# upstream endpoint keeps failing.
MAX_STALE_SECONDS = 24 * 3600
FETCH_TIMEOUT_SECONDS = 5.0

_MAX_AGE_RE = re.compile(r"max-age\s*=\s*(\d+)", re.IGNORECASE)


def parse_max_age(cache_control: object) -> int:
    """Return the TTL in seconds for a Cache-Control header value."""
    ttl = DEFAULT_TTL_SECONDS
    if isinstance(cache_control, str):
        match = _MAX_AGE_RE.search(cache_control)
        if match:
            digits = match.group(1)
            # Absurdly long digit strings are capped without int() parsing.
            ttl = int(digits) if len(digits) <= 9 else MAX_TTL_SECONDS
    return max(MIN_REFRESH_INTERVAL_SECONDS, min(ttl, MAX_TTL_SECONDS))


class JwksUnavailable(Exception):
    """Raised without fetching while a recent fetch failed and nothing is cached."""


class JwksCache:
    def __init__(self, url: str, clock: Callable[[], float] = time.monotonic) -> None:
        self._url = url
        self._clock = clock
        self._lock = asyncio.Lock()
        self.reset()

    def reset(self) -> None:
        self._jwks: dict | None = None
        self._kids: set[str] = set()
        self._expires_at = 0.0
        self._last_attempt: float | None = None
        self._last_failure: float | None = None

    def _can_serve(self, kid: str | None, now: float) -> bool:
        """True when the cache is unexpired and (if given) holds ``kid``."""
        return now < self._expires_at and (kid is None or kid in self._kids)

    def _within_stale_limit(self, now: float) -> bool:
        return now < self._expires_at + MAX_STALE_SECONDS

    async def get_jwks(self, kid: str | None = None) -> dict:
        """Return the key set, refreshing when expired or ``kid`` is unknown."""
        if self._jwks is not None and self._can_serve(kid, self._clock()):
            return self._jwks

        async with self._lock:
            now = self._clock()
            if self._jwks is not None and self._can_serve(kid, now):
                return self._jwks

            throttled = (
                self._last_attempt is not None
                and now - self._last_attempt < MIN_REFRESH_INTERVAL_SECONDS
            )
            if throttled and self._jwks is not None and self._within_stale_limit(now):
                return self._jwks

            usable = self._jwks is not None and self._within_stale_limit(now)
            if (
                not usable
                and self._last_failure is not None
                and now - self._last_failure < MIN_REFRESH_INTERVAL_SECONDS
            ):
                raise JwksUnavailable("recent JWKS fetch failed; not retrying yet")

            try:
                await self._fetch(now)
            except Exception:
                self._last_failure = now
                if self._jwks is not None and self._within_stale_limit(now):
                    logger.warning(
                        "JWKS refresh failed for %s; serving cached keys", self._url
                    )
                    return self._jwks
                raise
            return self._jwks  # type: ignore[return-value]

    async def _fetch(self, now: float) -> None:
        self._last_attempt = now
        logger.info("Fetching JWKS from %s", self._url)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(FETCH_TIMEOUT_SECONDS)
        ) as client:
            resp = await client.get(self._url)
            resp.raise_for_status()
            jwks = resp.json()
        if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
            raise TypeError("malformed JWKS response")
        keys = jwks["keys"]
        kids = {
            k["kid"]
            for k in keys
            if isinstance(k, dict) and isinstance(k.get("kid"), str)
        }
        ttl = parse_max_age(resp.headers.get("cache-control"))
        # Assign together only after everything validated.
        self._jwks, self._kids = jwks, kids
        self._expires_at = self._clock() + ttl
        self._last_failure = None
        logger.info("JWKS fetched — %d keys", len(keys))

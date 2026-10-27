"""Background task: periodically delete expired and revoked refresh tokens.

Expired tokens (expires_at < now) can never be used regardless of their
revocation status, so they are safe to remove.  Tokens revoked without being
rotated (logout, logout-all) are never read again and go too; rotated tokens
stay until they expire, because reuse detection needs them (#690).  Keeping
them indefinitely causes unbounded table growth and slows the JTI revocation
lookup index.  The purge runs in bounded batches
(app/auth/refresh_family.py:purge_refresh_tokens).

The same daily run also purges, in bounded batches:

- AI quota counters (``ai_quota``): day rows older than 90 days and month rows
  (no longer written since #582) older than 13 months (app/ai/quota.py);
- the AI usage ledger (``ai_usage``): raw rows older than 30 days are rolled
  up into ``ai_usage_daily`` and deleted, and daily rows older than 400 days
  are deleted (app/ai/ledger.py); in the same step, scan correction flags
  (``scan_corrections``, #631) older than 30 days and their daily totals
  (``scan_corrections_daily``) older than 400 days are deleted (each
  correction is counted into the daily table when it is written);
- the daily AI spend totals (``ai_spend_daily``) older than 90 days
  (app/ai/breaker.py);
- uncharged zero-result enhanced scans (``ai_enhanced_zero_results``) older
  than 30 days (app/ai/quota.py, #533);
- per-user scan records (``scan_records``) older than
  ``scan_records.retention_days`` (default 30) (app/ai/scan_records.py, #585).

The task runs once at startup (with an initial delay) then every 24 hours.
It is launched inside the FastAPI lifespan and cancelled on shutdown.

Each step is isolated (one failing never skips the others) and runs through
``_run_step``, which retries transient connection errors (any ``OSError``: DNS failure,
connection refused/reset, connect timeout; SQLAlchemy ``OperationalError`` /
``InterfaceError``) a
bounded number of times with exponential backoff (#661).  A blip that recovers
logs only a warning; a step that exhausts its retries, or fails with any other
error, logs one ``logger.exception`` so a persistent outage still reaches
Sentry.  ``asyncio.CancelledError`` is never caught.
"""

import asyncio
import logging
import socket
from collections.abc import Awaitable, Callable

from sqlalchemy.exc import InterfaceError, OperationalError

from app.ai.breaker import purge_old_spend
from app.ai.ledger import rollup_and_purge
from app.ai.quota import purge_old_quota, purge_old_zero_results, utc_today
from app.ai.scan_records import purge_old_scan_records
from app.auth.refresh_family import purge_refresh_tokens
from app.core.database import AsyncSessionLocal

logger = logging.getLogger(__name__)

_INTERVAL_SECONDS = 86_400  # 24 hours
_INITIAL_DELAY_SECONDS = 60  # wait a minute after startup before first run


_RETRY_ATTEMPTS = 3  # total tries per step (first try + 2 retries)
_RETRY_BASE_DELAY_SECONDS = 5.0  # backoff: 5s, then 10s

# OSError covers ConnectionError (refused/reset/aborted), socket.gaierror and
# TimeoutError; the subclasses are listed for readability.  CancelledError is
# not an OSError, so cancellation still propagates.
_TRANSIENT_ERRORS = (
    OSError,
    ConnectionError,
    socket.gaierror,
    TimeoutError,
    OperationalError,
    InterfaceError,
)


def _is_transient(exc: BaseException) -> bool:
    """True for connection-level blips worth retrying (checks the cause chain)."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        if isinstance(cur, _TRANSIENT_ERRORS):
            return True
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
    return False


async def _run_step(step: Callable[[], Awaitable[None]], label: str = "") -> None:
    """Run one purge step in isolation, retrying transient connection errors.

    ``label`` (e.g. ``"ai quota"``) is appended to the error message.  Never
    raises except ``asyncio.CancelledError`` (a BaseException, not caught here).
    """
    suffix = f" ({label})" if label else ""
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            await step()
            return
        except Exception as exc:
            if _is_transient(exc) and attempt < _RETRY_ATTEMPTS:
                delay = _RETRY_BASE_DELAY_SECONDS * 2 ** (attempt - 1)
                logger.warning(
                    "token_cleanup: transient DB error during cleanup run%s "
                    "(attempt %d/%d, %s); retrying in %.0fs",
                    suffix,
                    attempt,
                    _RETRY_ATTEMPTS,
                    type(exc).__name__,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            logger.exception("token_cleanup: error during cleanup run%s", suffix)
            return


async def _purge_quota() -> None:
    async with AsyncSessionLocal() as db:
        purged = await purge_old_quota(db)
        logger.info("token_cleanup: purged %d old ai_quota row(s)", purged)


async def _purge_usage() -> None:
    async with AsyncSessionLocal() as db:
        rolled, purged = await rollup_and_purge(db)
        logger.info(
            "token_cleanup: rolled up %d ai_usage row(s), "
            "purged %d ai_usage_daily row(s)",
            rolled,
            purged,
        )


async def _purge_spend() -> None:
    async with AsyncSessionLocal() as db:
        purged = await purge_old_spend(db, utc_today())
        logger.info("token_cleanup: purged %d old ai_spend_daily row(s)", purged)


async def _purge_zero_results() -> None:
    async with AsyncSessionLocal() as db:
        purged = await purge_old_zero_results(db)
        logger.info(
            "token_cleanup: purged %d old ai_enhanced_zero_results row(s)", purged
        )


async def _purge_scan_records() -> None:
    async with AsyncSessionLocal() as db:
        purged = await purge_old_scan_records(db)
        logger.info("token_cleanup: purged %d old scan_records row(s)", purged)


async def _purge_refresh_tokens() -> None:
    async with AsyncSessionLocal() as db:
        deleted = await purge_refresh_tokens(db)
        logger.info(
            "token_cleanup: deleted %d expired or revoked refresh token(s)", deleted
        )


async def cleanup_expired_tokens() -> None:
    """Long-running coroutine; meant to be wrapped in asyncio.create_task()."""
    await asyncio.sleep(_INITIAL_DELAY_SECONDS)

    while True:
        # Each step is isolated by _run_step: one failing must not skip the rest.
        await _run_step(_purge_quota, "ai quota")
        await _run_step(_purge_usage, "ai usage")
        await _run_step(_purge_spend, "ai spend")
        await _run_step(_purge_zero_results, "enhanced zero results")
        await _run_step(_purge_scan_records, "scan records")
        await _run_step(_purge_refresh_tokens)

        await asyncio.sleep(_INTERVAL_SECONDS)

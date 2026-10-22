"""Background task: periodically delete expired refresh tokens.

Expired tokens (expires_at < now) can never be used regardless of their
revocation status, so they are safe to remove.  Keeping them indefinitely
causes unbounded table growth and slows the JTI revocation lookup index.

The same daily run also purges, in bounded batches:

- the legacy scan usage counters (``scan_usage_daily``) older than 90 days
  (app/services/scan_quota.py, removed in #540);
- AI quota counters (``ai_quota``): day rows older than 90 days and month rows
  older than 13 months (app/ai/quota.py);
- the AI usage ledger (``ai_usage``): raw rows older than 30 days are rolled
  up into ``ai_usage_daily`` and deleted, and daily rows older than 400 days
  are deleted (app/ai/ledger.py);
- the daily AI spend totals (``ai_spend_daily``) older than 90 days
  (app/ai/breaker.py);
- uncharged zero-result enhanced scans (``ai_enhanced_zero_results``) older
  than 30 days (app/ai/quota.py, #533).

The task runs once at startup (with an initial delay) then every 24 hours.
It is launched inside the FastAPI lifespan and cancelled on shutdown.
"""

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import delete

from app.ai.breaker import purge_old_spend
from app.ai.ledger import rollup_and_purge
from app.ai.quota import purge_old_quota, purge_old_zero_results, utc_today
from app.core.database import AsyncSessionLocal
from app.models.refresh_token import RefreshToken
from app.services.scan_quota import purge_old_scan_usage

logger = logging.getLogger(__name__)

_INTERVAL_SECONDS = 86_400  # 24 hours
_INITIAL_DELAY_SECONDS = 60  # wait a minute after startup before first run


async def cleanup_expired_tokens() -> None:
    """Long-running coroutine; meant to be wrapped in asyncio.create_task()."""
    await asyncio.sleep(_INITIAL_DELAY_SECONDS)

    while True:
        # Separate try blocks: one purge failing must not skip the other.
        try:
            async with AsyncSessionLocal() as db:
                purged = await purge_old_scan_usage(db)
                logger.info("token_cleanup: purged %d old scan usage row(s)", purged)
        except Exception:
            logger.exception("token_cleanup: error during cleanup run (scan usage)")

        try:
            async with AsyncSessionLocal() as db:
                purged = await purge_old_quota(db)
                logger.info("token_cleanup: purged %d old ai_quota row(s)", purged)
        except Exception:
            logger.exception("token_cleanup: error during cleanup run (ai quota)")

        try:
            async with AsyncSessionLocal() as db:
                rolled, purged = await rollup_and_purge(db)
                logger.info(
                    "token_cleanup: rolled up %d ai_usage row(s), "
                    "purged %d ai_usage_daily row(s)",
                    rolled,
                    purged,
                )
        except Exception:
            logger.exception("token_cleanup: error during cleanup run (ai usage)")

        try:
            async with AsyncSessionLocal() as db:
                purged = await purge_old_spend(db, utc_today())
                logger.info(
                    "token_cleanup: purged %d old ai_spend_daily row(s)", purged
                )
        except Exception:
            logger.exception("token_cleanup: error during cleanup run (ai spend)")

        try:
            async with AsyncSessionLocal() as db:
                purged = await purge_old_zero_results(db)
                logger.info(
                    "token_cleanup: purged %d old ai_enhanced_zero_results row(s)",
                    purged,
                )
        except Exception:
            logger.exception(
                "token_cleanup: error during cleanup run (enhanced zero results)"
            )

        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    delete(RefreshToken).where(
                        RefreshToken.expires_at < datetime.now(UTC)
                    )
                )
                await db.commit()
                logger.info(
                    "token_cleanup: deleted %d expired refresh token(s)",
                    result.rowcount,
                )
        except Exception:
            logger.exception("token_cleanup: error during cleanup run")

        await asyncio.sleep(_INTERVAL_SECONDS)

"""Background task: periodically delete expired refresh tokens.

Expired tokens (expires_at < now) can never be used regardless of their
revocation status, so they are safe to remove.  Keeping them indefinitely
causes unbounded table growth and slows the JTI revocation lookup index.

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
"""

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import delete

from app.ai.breaker import purge_old_spend
from app.ai.ledger import rollup_and_purge
from app.ai.quota import purge_old_quota, purge_old_zero_results, utc_today
from app.ai.scan_records import purge_old_scan_records
from app.core.database import AsyncSessionLocal
from app.models.refresh_token import RefreshToken

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
                purged = await purge_old_scan_records(db)
                logger.info("token_cleanup: purged %d old scan_records row(s)", purged)
        except Exception:
            logger.exception("token_cleanup: error during cleanup run (scan records)")

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

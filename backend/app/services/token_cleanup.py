"""Background task: periodically delete expired refresh tokens.

Expired tokens (expires_at < now) can never be used regardless of their
revocation status, so they are safe to remove.  Keeping them indefinitely
causes unbounded table growth and slows the JTI revocation lookup index.

The task runs once at startup (with an initial delay) then every 24 hours.
It is launched inside the FastAPI lifespan and cancelled on shutdown.
"""

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import delete

from app.core.database import AsyncSessionLocal
from app.models.refresh_token import RefreshToken

logger = logging.getLogger(__name__)

_INTERVAL_SECONDS = 86_400  # 24 hours
_INITIAL_DELAY_SECONDS = 60  # wait a minute after startup before first run


async def cleanup_expired_tokens() -> None:
    """Long-running coroutine; meant to be wrapped in asyncio.create_task()."""
    await asyncio.sleep(_INITIAL_DELAY_SECONDS)

    while True:
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

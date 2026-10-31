"""Give-backs: never raise, bounded, shielded from cancellation."""

import logging
from collections.abc import Awaitable, Callable

import anyio
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import quota
from app.ai.quota.constants import (
    GIVE_BACK_DONE,
    GIVE_BACK_FAILED,
    GIVE_BACK_TIMED_OUT,
)

logger = logging.getLogger(__package__)


async def _give_back(
    db: AsyncSession,
    what: str,
    op: Callable[[], Awaitable[object]],
    *,
    attempts: int = 2,
) -> str:
    """Run a give-back so that it cannot mask the original outcome.

    Shielded so it still runs while the request is being cancelled (anyio
    cancellation, as on the request path; a native ``asyncio`` task cancel,
    e.g. at server shutdown, cuts through the shield), and bounded by ``CLEANUP_TIMEOUT_SECONDS``. A failure is logged and retried
    once after a rollback (the session may be in a failed transaction after
    the error being handled); a second failure is logged and swallowed: the
    lease expiry is the backstop. ``attempts=1`` is for an operation that is
    not safe to repeat: if it raised after its commit (an acknowledgement
    lost), a retry would apply it twice.

    If the bound cuts a query off mid-execute, SQLAlchemy invalidates the
    session's connection and every later query on the request session would
    raise ``PendingRollbackError``. So after a timeout the session is rolled
    back (also shielded and bounded; a failure there is logged and swallowed).

    Returns :data:`GIVE_BACK_DONE`, :data:`GIVE_BACK_FAILED` (every attempt
    raised) or :data:`GIVE_BACK_TIMED_OUT` (the bound cut it off).
    """
    with anyio.move_on_after(quota.CLEANUP_TIMEOUT_SECONDS, shield=True) as scope:
        for attempt in range(1, attempts + 1):
            try:
                await op()
                return GIVE_BACK_DONE
            except Exception:
                logger.exception("ai_quota: %s failed (attempt %d)", what, attempt)
                try:
                    await db.rollback()
                except Exception:
                    logger.exception("ai_quota: rollback after failed %s", what)
    if scope.cancelled_caught:
        logger.error("ai_quota: %s timed out", what)
        with anyio.move_on_after(
            quota.CLEANUP_TIMEOUT_SECONDS, shield=True
        ) as rb_scope:
            try:
                await db.rollback()
            except Exception:
                logger.exception("ai_quota: rollback after timed-out %s", what)
        if rb_scope.cancelled_caught:
            logger.error("ai_quota: rollback after timed-out %s timed out", what)
        return GIVE_BACK_TIMED_OUT
    return GIVE_BACK_FAILED

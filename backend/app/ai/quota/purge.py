"""Retention: bounded purges of old quota and zero-result rows."""

from datetime import date, datetime, timedelta

from sqlalchemy import delete, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import quota
from app.models.ai_enhanced_zero_result import AIEnhancedZeroResult
from app.models.ai_quota import WINDOW_DAY, WINDOW_MONTH, AIQuota


async def _purge_kind(db: AsyncSession, window_kind: str, cutoff: date) -> int:
    total = 0
    for _ in range(quota.PURGE_MAX_BATCHES):
        batch = (
            select(
                AIQuota.user_id,
                AIQuota.feature,
                AIQuota.window_kind,
                AIQuota.window_start,
            )
            .where(AIQuota.window_kind == window_kind, AIQuota.window_start < cutoff)
            .limit(quota.PURGE_BATCH_SIZE)
        )
        result = await db.execute(
            delete(AIQuota).where(
                tuple_(
                    AIQuota.user_id,
                    AIQuota.feature,
                    AIQuota.window_kind,
                    AIQuota.window_start,
                ).in_(batch)
            )
        )
        await db.commit()
        deleted = result.rowcount or 0
        total += deleted
        if deleted < quota.PURGE_BATCH_SIZE:
            break
    return total


def purge_cutoffs(today: date) -> tuple[date, date]:
    """``(day_cutoff, month_cutoff)``: rows with ``window_start`` before these
    are purged. Day rows are kept for 90 days; month rows (no longer written
    since #582) for the current month and the 13 before it, so they age out
    and a rollback to the previous release finds its counters intact."""
    return (
        today - timedelta(days=quota.DAILY_RETENTION_DAYS),
        quota.months_before(today, quota.MONTHLY_RETENTION_MONTHS),
    )


async def purge_old_quota(db: AsyncSession, today: date | None = None) -> int:
    """Delete old day and month rows in bounded batches; return the count."""
    day_cutoff, month_cutoff = quota.purge_cutoffs(today or quota.utc_today())
    deleted = await quota._purge_kind(db, WINDOW_DAY, day_cutoff)
    deleted += await quota._purge_kind(db, WINDOW_MONTH, month_cutoff)
    return deleted


async def purge_old_zero_results(db: AsyncSession, now: datetime | None = None) -> int:
    """Delete ``ai_enhanced_zero_results`` rows older than 30 days in bounded
    batches; return the count."""
    cutoff = (now or quota.utc_now()) - timedelta(days=quota.ZERO_RESULT_RETENTION_DAYS)
    total = 0
    for _ in range(quota.PURGE_MAX_BATCHES):
        batch = (
            select(AIEnhancedZeroResult.id)
            .where(AIEnhancedZeroResult.created_at < cutoff)
            .limit(quota.PURGE_BATCH_SIZE)
        )
        result = await db.execute(
            delete(AIEnhancedZeroResult).where(AIEnhancedZeroResult.id.in_(batch))
        )
        await db.commit()
        deleted = result.rowcount or 0
        total += deleted
        if deleted < quota.PURGE_BATCH_SIZE:
            break
    return total

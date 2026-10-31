"""/scan requests per user per UTC day (#586)."""

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import quota
from app.ai.quota.constants import SCAN_REQUESTS_QUOTA_FEATURE
from app.ai.quota.types import ScanRequestsDailyLimitReached
from app.ai.settings import AISettings
from app.models.ai_quota import WINDOW_DAY


async def count_scan_request(
    db: AsyncSession,
    user_id: uuid.UUID,
    ai: AISettings,
    now: datetime | None = None,
) -> int:
    """Count one /scan request for ``user_id`` today (UTC) and return the
    new count, or raise :class:`ScanRequestsDailyLimitReached` (nothing
    written) when ``scan.requests_per_user_daily`` requests were already
    made today.

    The same exact conditional upsert as the daily unit (``units_used + 1
    WHERE units_used < limit``, committed straight away), so parallel
    requests on any number of instances never pass the limit. It counts
    requests, not scans: nothing is ever given back, whatever happens to
    the request afterwards. Not a lease or allowance step and not an AI
    call, so it is kept out of :func:`global_units_today`."""
    now = now or quota.utc_now()
    used = await quota.reserve_unit(
        db,
        user_id,
        SCAN_REQUESTS_QUOTA_FEATURE,
        WINDOW_DAY,
        quota.utc_today(now),
        ai.scan_requests_per_user_daily,
    )
    if used is None:
        raise ScanRequestsDailyLimitReached(
            retry_after=quota.seconds_until_utc_midnight(now)
        )
    return used

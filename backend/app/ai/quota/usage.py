"""Usage (GET /scan/usage) and the enhanced scans left today."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import entitlements, quota
from app.ai.entitlements import Tier
from app.ai.quota.constants import SCAN_NO_MATCH_QUOTA_FEATURE, SCAN_QUOTA_FEATURE
from app.ai.quota.types import Reservation
from app.ai.settings import AISettings
from app.models.ai_quota import WINDOW_DAY
from app.models.user import User


@dataclass(frozen=True)
class Usage:
    """Found scans reserved today (UTC) and the tier's found allowance
    (``used_today`` / ``daily_limit``), and the no-match counter and its
    ceiling (#577). There is no monthly count or limit (#582)."""

    used_today: int
    daily_limit: int
    no_match_used_today: int = 0
    no_match_daily_limit: int = 0


async def usage_for(
    db: AsyncSession,
    *,
    user: User,
    feature: str,
    ai: AISettings,
    now: datetime | None = None,
) -> Usage:
    tier = await entitlements.tier_for(db, user)
    allowance = quota.allowance_for(ai, tier)
    today = quota.utc_today(now)
    used = await quota.units_used(db, user.id, feature, WINDOW_DAY, today)
    no_match = await quota.units_used(
        db, user.id, SCAN_NO_MATCH_QUOTA_FEATURE, WINDOW_DAY, today
    )
    return Usage(
        used_today=used,
        daily_limit=allowance.daily,
        no_match_used_today=no_match,
        no_match_daily_limit=allowance.no_match,
    )


async def enhanced_remaining(db: AsyncSession, reservation: Reservation) -> int | None:
    """Enhanced scans the user can still make today after the scan: the
    shared daily allowance left, since an enhanced scan takes the shared
    daily unit and nothing else (#582; #583 adds a daily Sonnet counter).
    ``None`` for every tier but Premium (enhanced is Premium-only, #533).
    Ends its read transaction."""
    if reservation.tier is not Tier.PREMIUM:
        return None
    try:
        used = await quota.units_used(
            db,
            reservation.user_id,
            reservation.feature,
            WINDOW_DAY,
            reservation.day,
        )
    finally:
        await db.commit()
    return max(0, reservation.allowance.daily - used)


async def enhanced_remaining_for(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    tier: Tier,
    ai: AISettings,
    now: datetime | None = None,
) -> int | None:
    """As :func:`enhanced_remaining`, with no reservation (a repeat served
    from a scan record, #585, which takes nothing): ``None`` for every tier
    but Premium, else today's shared daily allowance left. Ends its read
    transaction."""
    if tier is not Tier.PREMIUM:
        return None
    try:
        used = await quota.units_used(
            db, user_id, SCAN_QUOTA_FEATURE, WINDOW_DAY, quota.utc_today(now)
        )
    finally:
        await db.commit()
    return max(0, quota.allowance_for(ai, tier).daily - used)

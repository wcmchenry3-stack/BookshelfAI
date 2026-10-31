"""Reserve: the allowance checks and takes before the provider call."""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import breaker, entitlements, quota
from app.ai.entitlements import Tier
from app.ai.quota.types import (
    CostCeilingReached,
    DailyLimitReached,
    EnhancedRequiresPremium,
    EnhancedThrottled,
    GlobalCapacityReached,
    Reservation,
    ScanInProgress,
    _Unit,
)
from app.ai.settings import ZERO_RESULT_ACTION_THROTTLE, AISettings
from app.models.user import User


async def require_enhanced_entitlement(
    db: AsyncSession, user: User, enhanced: bool
) -> Tier:
    """The user's tier (``entitlements.tier_for``, server-side only); raises
    :class:`EnhancedRequiresPremium` for an enhanced scan by any tier but
    Premium (#533). Used by :func:`reserve` and by ``/scan`` before a scan
    record is served (#585), so feature gating applies to repeats too.
    Takes nothing."""
    tier = await entitlements.tier_for(db, user)
    if enhanced and tier is not Tier.PREMIUM:
        raise EnhancedRequiresPremium("enhanced_requires_premium")
    return tier


async def reserve(
    db: AsyncSession,
    *,
    user: User,
    feature: str,
    timeout_s: float,
    ai: AISettings,
    est_cost_micros: int,
    now: datetime | None = None,
    enhanced: bool = False,
) -> Reservation:
    """Check the allowances and take the cost estimate, the lease and a
    daily unit. An enhanced scan (#533) takes the same daily unit; there is
    no monthly or enhanced monthly unit (#582).

    ``est_cost_micros`` is the gateway feature's
    ``FeatureSpec.est_cost_ceiling_micros`` (server-side registry value).

    Raises a :class:`QuotaRefusal` (after giving back whatever was taken) when
    the call must not go ahead. Any other exception also gives back what was
    taken before it propagates. On success the caller owns the reservation and
    must :func:`release` it after the provider call.
    """
    # Before anything is taken (#533).
    tier = await quota.require_enhanced_entitlement(db, user, enhanced)
    allowance = quota.allowance_for(ai, tier)
    today = quota.utc_today(now)

    if await quota.global_units_today(db, today) >= ai.global_daily_limit:
        raise GlobalCapacityReached("scan_capacity")

    # Free pauses at the ceiling; Premium (and only Premium) is not gated by
    # it but still adds its estimate (#544). Any other tier is gated.
    ceiling = ai.free_daily_ceiling_micros
    if not await breaker.reserve(
        db, today, est_cost_micros, ceiling, enforce=tier is not Tier.PREMIUM
    ):
        raise CostCeilingReached("ai_budget_reached")

    async def give_back_cost() -> None:
        # One attempt only: there is no lease or ledger row yet to show
        # whether a failed attempt committed, and a repeated release would
        # give back another call's estimate. A failure leaves the estimate
        # reserved until the day ends (the breaker refuses sooner, not later).
        await quota._give_back(
            db,
            "cost release",
            lambda: breaker.release(db, today, est_cost_micros),
            attempts=1,
        )

    seconds = quota.lease_seconds(timeout_s)
    try:
        lease_id = await quota.acquire_lease(db, user.id, seconds)
    except BaseException:
        await give_back_cost()
        raise
    if lease_id is None:
        await give_back_cost()
        raise ScanInProgress("scan_in_progress")
    reservation = Reservation(
        user_id=user.id,
        feature=feature,
        lease_id=lease_id,
        day=today,
        tier=tier,
        allowance=allowance,
        est_cost_micros=est_cost_micros,
        ceiling_micros=ceiling,
        enhanced=enhanced,
        zero_result_threshold=ai.zero_result_threshold,
        zero_result_window_hours=ai.zero_result_window_hours,
        zero_result_action=ai.zero_result_action,
    )

    daily = quota._daily_unit(reservation)
    # Units whose take was attempted, and whether it was acknowledged. After
    # a lost acknowledgement the local state says "attempted, not taken"; the
    # flag on the lease row is the truth (#590).
    attempted: list[_Unit] = []
    taken: set[str] = set()

    async def take(unit: _Unit, limit: int) -> int | None:
        attempted.append(unit)
        used = await quota.take_unit(
            db,
            user.id,
            lease_id,
            unit.flag,
            unit.feature,
            unit.window_kind,
            unit.window_start,
            limit,
        )
        if used is None:
            # Refused, and the refusal was acknowledged: nothing was taken.
            attempted.remove(unit)
        else:
            taken.add(unit.flag)
        return used

    try:
        if enhanced and reservation.zero_result_action == ZERO_RESULT_ACTION_THROTTLE:
            retry_after = await quota.zero_result_throttle(
                db,
                user.id,
                reservation.zero_result_threshold,
                reservation.zero_result_window_hours,
            )
            if retry_after is not None:
                raise EnhancedThrottled(retry_after)
        if await take(daily, allowance.daily) is None:
            raise DailyLimitReached(retry_after=quota.seconds_until_utc_midnight(now))
        # The upserts above are unbounded, and the lease aged while they ran.
        # Restart its clock so the whole timeout_s + 30 s covers the provider
        # call, and stop if a stall let it expire and another request take it.
        if not await quota.renew_lease(db, user.id, lease_id, seconds):
            raise ScanInProgress("scan_in_progress")
    except BaseException:
        # Includes CancelledError and a take whose commit acknowledgement was
        # lost: give back the unit if its take was attempted, gated on the
        # lease row's flag (a refused or never-landed take
        # gives nothing back), then propagate.
        for unit in reversed(attempted):
            await quota._return_unit(db, reservation, unit, taken=unit.flag in taken)
        # Releases the lease and gives back the cost estimate.
        await quota.release(db, reservation)
        raise
    return reservation

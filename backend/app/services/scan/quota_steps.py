"""The scan's quota steps (``app/ai/quota/``) mapped to HTTP refusals: the
per-day request count (#586), the enhanced entitlement (#533), the
reservation, its refund and the lease release (settle), and the settle-time
decisions (zero-result, no-match). Every call goes through ``quota.X`` at
call time, so a patch of the ``app.ai.quota`` attribute reaches it.

Refund and settle policy, as applied by the provider call
(``app/api/scan.py:_run_provider``):

The reservation is given back only when the provider reports itself
unavailable or was never reached (connection not established). Any other
failure (or a cancelled request) keeps it: the provider may already have
been paid. An empty result also counts. The lease is always released once
the provider call is over (settle), even on cancellation.

A malformed reply (#572: the provider answered HTTP 200, billed, but the
reply was missing, not JSON, of the wrong shape or ended on an unexpected
stop reason, or it was cut off by the token limit before a single complete
book) is treated like unavailability for the user: 503 scan_unavailable and
the daily unit refunded (the only unit, #582), never a zero-result and never
saved as a scan record. Its real token usage is still recorded (ledger
status ``malformed``) and its priced cost settled, because the provider
billed it.

Enhanced zero-result record (#533): an enhanced scan takes only the shared
daily unit (#582). When the provider answered but returned no books (no
candidates), the settle records it in its own transaction (quota.release
with a ZeroResult): below the user's zero-result threshold one
ai_enhanced_zero_results row is written (the abuse signal, with its warning
and the throttle); the daily unit stays charged either way. The decision is
on the provider's candidates, so enrichment stays after the settle (the
lease covers only the provider call); enrichment returns a book for every
candidate it does not crash on. A provider error, timeout or unavailability
is never a zero-result. A repeat served from a scan record (#585) returned
before the reservation: it made no provider call and records none.

Found / no-match (#577): a standard scan whose provider answered properly
(outcome ok) with no candidates passes a NoMatch to the settle, which moves
the found unit to the no-match counter when it has room (in the settle
transaction, under the same gate, at most once). The response says what
happened (``no_match``: ``free`` or ``counted``). Enhanced scans, refusals
and truncated replies never move; malformed replies were already refunded.

Usage ledger (#530): every outcome of the provider call writes one ai_usage
row, committed with the lease release. call_id is generated once per call
(in scan()), so a retried settle cannot write it twice. A kill switch hit
(FeatureDisabledError) never reached a provider and writes none.

Cost (#531): the settle also moves the estimate out of today's reserved
spend. A refunded call (unavailable, never reached, kill switch) settles
nothing; an answered call settles its priced cost; a timed-out, failed or
cancelled call keeps the estimate as spend (real cost unknown, likely
billed).
"""

import logging
import uuid

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Executable

from app.ai import breaker, quota
from app.ai.entitlements import Tier
from app.ai.gateway import AIResult
from app.ai.registry import get_feature
from app.ai.settings import AISettings
from app.models.ai_usage import STATUS_OK
from app.models.user import User
from app.services.scan import SCAN_LOGGER

logger = logging.getLogger(SCAN_LOGGER)


async def reserve_scan_quota(
    db: AsyncSession, user: User, feature: str, ai: AISettings, enhanced: bool
) -> quota.Reservation:
    """Global count cap, then the daily cost ceiling, then the in-flight
    lease, then the daily reservation (``app/ai/quota/``), mapped to HTTP
    refusals. For ``enhanced`` (#533), first the Premium-only refusal (before
    anything is taken), then after the lease the zero-result throttle. There
    is no monthly reservation and no 402 (#582).

    ``feature`` (the gateway feature) sets the lease length from its timeout
    and the cost estimate from its ``est_cost_ceiling_micros``; the units are reserved under ``quota.SCAN_QUOTA_FEATURE``, the
    one allowance standard and enhanced scans share.

    Enhanced scans are Premium-only: a non-Premium user gets 403
    enhanced_requires_premium inside quota.reserve before anything is
    reserved (the tier is resolved server-side there, never from the
    request). There is no monthly or enhanced monthly allowance (#582), so
    nothing is refused for a monthly reason. At the cost ceiling Free gets
    503 ai_budget_reached; Premium continues. Every refusal here happens
    before the provider and gives back what it took.

    Runs on the request's own session; each step commits, so the connection
    is back in the pool during the provider call. A refusal has already given
    back anything taken. Only the refusal classes below are caught: anything
    else propagates (a 500 before any provider call, i.e. fail closed).
    """
    spec = get_feature(feature)
    try:
        return await quota.reserve(
            db,
            user=user,
            feature=quota.SCAN_QUOTA_FEATURE,
            timeout_s=spec.timeout_s,
            ai=ai,
            est_cost_micros=spec.est_cost_ceiling_micros,
            enhanced=enhanced,
        )
    except quota.EnhancedRequiresPremium:
        raise enhanced_requires_premium() from None
    except quota.EnhancedThrottled as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="enhanced_throttled",
            headers={"Retry-After": str(exc.retry_after)},
        ) from None
    except quota.CostCeilingReached:
        # Free tier paused for the rest of the UTC day (#531, #544).
        logger.warning("scan: daily AI cost ceiling reached; free tier paused")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ai_budget_reached",
        ) from None
    except quota.GlobalCapacityReached:
        logger.warning("scan: global daily limit reached")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="scan_capacity",
        ) from None
    except quota.ScanInProgress as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="scan_in_progress",
            headers={"Retry-After": str(exc.retry_after)},
        ) from None
    except quota.DailyLimitReached as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="daily_scan_limit",
            headers={"Retry-After": str(exc.retry_after)},
        ) from None


async def count_scan_request(db: AsyncSession, user: User, ai: AISettings) -> None:
    """Count this /scan request for the user's UTC day (#586); 429
    ``scan_requests_daily_limit`` with a Retry-After until UTC midnight when
    the day's requests are used up. Distinct from the per-minute 429 and
    from ``daily_scan_limit``.

    Every request past authentication (get_current_user) and the kill
    switches counts, new photo or repeat, allowed or refused later; nothing
    is given back. At scan.requests_per_user_daily: 429
    scan_requests_daily_limit until UTC midnight, before any other work
    (Turnstile, the upload, normalisation, the scan-record lookup, the
    allowance and the provider). The kill switches stay first: they cost
    nothing (no DB write, no work), a switched-off scan should say so, and
    the app's retries during an outage we caused should not use up a
    user's requests. Turnstile comes after: it is an outbound call, i.e.
    work this limit bounds, and a request that fails it is the kind of
    request the limit is for. Repeats are free (#585), so this is what
    stops someone hammering the endpoint (decision #573 point 3)."""
    try:
        await quota.count_scan_request(db, user.id, ai)
    except quota.ScanRequestsDailyLimitReached as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="scan_requests_daily_limit",
            headers={"Retry-After": str(exc.retry_after)},
        ) from None


def enhanced_requires_premium() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="enhanced_requires_premium",
    )


async def require_enhanced_entitlement(
    db: AsyncSession, user: User, enhanced: bool
) -> Tier:
    """The user's tier (server-side); 403 ``enhanced_requires_premium`` for
    an enhanced scan by a non-Premium user (#533). Checked before a repeat
    is served from a scan record, so a repeat is gated like a new photo
    (#585); takes nothing."""
    try:
        return await quota.require_enhanced_entitlement(db, user, enhanced)
    except quota.EnhancedRequiresPremium:
        raise enhanced_requires_premium() from None


async def refund_scan_quota(db: AsyncSession, reservation: quota.Reservation) -> None:
    """Give back the daily unit; never raises."""
    await quota.refund(db, reservation)


async def release_scan_lease(
    db: AsyncSession,
    reservation: quota.Reservation,
    ledger_row: Executable | None = None,
    settlement: breaker.Settlement | None = None,
    zero_result: quota.ZeroResult | None = None,
    no_match: quota.NoMatch | None = None,
) -> quota.NoMatchMoved | None:
    """Release the in-flight lease (settle) and commit the call's usage-ledger
    row, cost settle and (an enhanced scan with no books, #533) the charge
    decision or (a standard scan with no books, #577) the no-match move in
    the same transaction; never raises, shielded. Returns the no-match
    decision, if one was committed."""
    return await quota.release(
        db, reservation, ledger_row, settlement, zero_result, no_match
    )


def settle_decisions(
    result: AIResult,
    *,
    enhanced: bool,
    call_id: uuid.UUID,
    ledger_row: Executable | None,
) -> tuple[quota.ZeroResult | None, quota.NoMatch | None]:
    """What the settle decides for an answered, usable call: an enhanced
    zero-result (#533) or a standard no-match (#577), or neither. Pure; never
    raises.

    A refusal is still a zero-book answer here: refusals are out of scope for
    #572, see the comment on #572 about the limits-matrix row
    premium/enhanced/provider_refused_below_threshold. A truncated reply with
    no books never gets here: the gateway reports it as ``malformed`` (owner
    decision on #572)."""
    zero_result: quota.ZeroResult | None = None
    no_match: quota.NoMatch | None = None
    if enhanced and not result.candidates:
        # The provider answered with no books: the settle decides.
        zero_result = quota.ZeroResult(
            ledger_id=call_id if ledger_row is not None else None
        )
    if not enhanced and result.outcome == STATUS_OK and not result.candidates:
        # A genuine no-match (#577): the settle decides the move.
        no_match = quota.NoMatch()
    return zero_result, no_match


async def enhanced_remaining(
    db: AsyncSession, reservation: quota.Reservation
) -> int | None:
    """The remaining enhanced allowance for the response; None for
    non-Premium, or if it cannot be read (never fails the scan)."""
    try:
        return await quota.enhanced_remaining(db, reservation)
    except Exception:
        logger.exception("scan: could not read the remaining enhanced allowance")
        try:
            await db.rollback()
        except Exception:
            logger.exception("scan: rollback after the allowance read failed")
        return None


def no_match_field(moved: object) -> str | None:
    """``ScanResponse.no_match`` from the settle's no-match decision (#577);
    None when there was none. Never raises."""
    if isinstance(moved, quota.NoMatchMoved) and moved.outcome in (
        quota.NO_MATCH_FREE,
        quota.NO_MATCH_COUNTED,
    ):
        return moved.outcome
    return None

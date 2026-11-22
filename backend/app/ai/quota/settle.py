"""The settle: :func:`release` the lease, with the ledger row and cost."""

import logging

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Executable

from app.ai import breaker, quota
from app.ai.quota.constants import (
    GIVE_BACK_DONE,
    GIVE_BACK_FAILED,
    GIVE_BACK_TIMED_OUT,
)
from app.ai.quota.types import (
    NoMatch,
    NoMatchMoved,
    Reservation,
    ZeroResult,
    ZeroResultRecorded,
)

logger = logging.getLogger(__package__)


async def release(
    db: AsyncSession,
    reservation: Reservation,
    ledger_row: Executable | None = None,
    settlement: breaker.Settlement | None = None,
    zero_result: ZeroResult | None = None,
    no_match: NoMatch | None = None,
) -> NoMatchMoved | None:
    """Release the lease held by ``reservation`` (settle). Never raises.

    ``no_match`` (#577, standard reservations only): the provider answered
    with no books, so the no-match move (:func:`move_to_no_match`) runs in
    the same transaction as the cost settle and under the same gate. Returns
    what it decided once the settle committed, else ``None`` (no
    ``no_match``, an enhanced reservation, a retried or failed settle, or a
    lost lease: the scan stays charged). The ceiling warning is sent after
    the commit.

    ``zero_result`` (#533, enhanced reservations only): the scan found no
    books, so the zero-result record (:func:`record_zero_result`) runs in the
    same transaction as the cost settle and under the same gate, and a
    warning is sent after the commit when the count reaches the threshold.

    ``ledger_row`` (#530) is the usage-ledger insert for the provider call
    (``app.ai.ledger.usage_insert``). It is executed on the same session just
    before the lease delete, so :func:`release_lease`'s commit writes both:
    the ledger row and the settle land in one transaction. The pair is
    bounded, shielded and retried like any give-back. If every attempt fails
    with an error (for example the row itself is rejected by a CHECK or FK),
    the ledger row is dropped and the lease is released on its own, so a
    ledger problem can never keep the lease held or mask the scan's outcome;
    the failure is logged (no content). If the attempt was cut off by the
    bound, the database is slow rather than the row bad: no further attempt
    is made (the settle stays within one bound) and the lease expiry is the
    backstop.

    The cost settle (#531, :func:`app.ai.breaker.settle`) is executed in the
    same transaction: the reservation's estimate leaves ``reserved`` and
    ``settlement.cost_micros`` (0 without a settlement: the estimate is
    given back) is added to ``settled``. It also runs in the lease-only
    fallback, so a dropped ledger row never leaves the estimate reserved.
    After the commit, any spend alert threshold the settle crossed is claimed
    and sent (:func:`_alert`).

    The ledger insert is idempotent (``ON CONFLICT (call_id) DO NOTHING
    RETURNING call_id``), so a retry after a commit whose acknowledgement was
    lost adds no second row; when it returns no row the cost settle is
    skipped too, since it was committed with that row (#531). Without a
    ledger row (refusals in :func:`reserve`, and the lease-only fallback) the
    lease is the proof instead: it is deleted in the same commit as the
    spend, so a retry deletes the lease first and settles the spend only if
    it was still there. If the lease was lost to another request for some
    other reason, the retry leaves the estimate reserved until the day ends,
    which can only make the breaker refuse sooner.
    """
    est = reservation.est_cost_micros
    cost = settlement.cost_micros if settlement is not None else 0
    settled: list[tuple[int, int, int] | None] = []
    recorded: list[ZeroResultRecorded] = []
    moved: list[NoMatchMoved] = []
    attempts = 0
    decide = zero_result is not None and reservation.enhanced
    # Enhanced scans never move (#577: the #533 zero-result rule stays).
    move = no_match is not None and not reservation.enhanced

    async def settle_spend() -> None:
        settled.clear()
        recorded.clear()
        moved.clear()
        if est or cost:
            settled.append(await breaker.settle(db, reservation.day, est, cost))
        if decide:
            count = await quota.record_zero_result(
                db, reservation, zero_result.ledger_id
            )
            if count is not None:
                recorded.append(count)
        if move:
            decision = await quota.move_to_no_match(db, reservation)
            if decision is not None:
                moved.append(decision)

    async def settle_after_lease() -> None:
        # The lease delete commits on its own here, then the spend. If the
        # spend then fails, the estimate stays reserved (fails safe).
        settled.clear()
        recorded.clear()
        moved.clear()
        if await quota.release_lease(db, reservation.user_id, reservation.lease_id):
            await settle_spend()
            await db.commit()

    async def settle() -> None:
        nonlocal attempts
        attempts += 1
        if ledger_row is None and attempts > 1:
            # A retry: the first attempt may have committed (its
            # acknowledgement lost) with nothing but the lease to show it.
            await settle_after_lease()
            return
        inserted = True
        if ledger_row is not None:
            result = await db.execute(ledger_row)
            if est or cost or decide or move:
                # ON CONFLICT (call_id) DO NOTHING RETURNING call_id: no row
                # back means an earlier attempt already committed this call's
                # row, and with it the spend settle and the lease delete (a
                # commit whose acknowledgement was lost). Do not add it again.
                inserted = result.scalar_one_or_none() is not None
        if inserted:
            await settle_spend()
        else:
            settled.clear()
            recorded.clear()
            moved.clear()
        await quota.release_lease(db, reservation.user_id, reservation.lease_id)

    if ledger_row is None:
        outcome = await quota._give_back(db, "lease release", settle)
    else:
        outcome = await quota._give_back(db, "lease release with usage row", settle)
        if outcome == GIVE_BACK_TIMED_OUT:
            logger.error(
                "ai_quota: settle timed out; the lease is left to expire, "
                "and the usage row may be missing"
            )
            return None
        if outcome == GIVE_BACK_FAILED:
            logger.error("ai_quota: usage row dropped; releasing the lease without it")
            outcome = await quota._give_back(db, "lease release", settle_after_lease)
    if outcome == GIVE_BACK_DONE and settlement is not None and settled:
        await quota._alert(db, reservation, settlement, settled[0])
    if outcome == GIVE_BACK_DONE and recorded and recorded[0].warn:
        # The row that claimed the warning (warned = true) is committed, so
        # it is sent at most once per user per window.
        quota.send_zero_result_warning(
            count=recorded[0].count,
            threshold=reservation.zero_result_threshold,
            window_hours=reservation.zero_result_window_hours,
            action=reservation.zero_result_action,
        )
    if outcome != GIVE_BACK_DONE or not moved:
        return None
    if moved[0].warn:
        # Claimed by the move that brought the count to the ceiling, now
        # committed: at most once per user per day.
        quota.send_no_match_ceiling_warning(
            tier=reservation.tier, ceiling=reservation.allowance.no_match
        )
    return moved[0]


async def _alert(
    db: AsyncSession,
    reservation: Reservation,
    settlement: breaker.Settlement,
    totals: tuple[int, int, int] | None,
) -> None:
    """Claim and send each spend alert this settle crossed (#531). Never
    raises; bounded and shielded like a give-back. A threshold already sent
    today is skipped without a query."""
    if totals is None:
        return
    settled_micros, reserved_micros, mask = totals
    pending = breaker.pending_alerts(settled_micros, reservation.ceiling_micros, mask)
    if not pending:
        return
    won: list[int] = []

    async def claim() -> None:
        won[:] = await breaker.claim_alerts(db, reservation.day, pending)

    if await quota._give_back(db, "spend alert claim", claim) != GIVE_BACK_DONE:
        return
    for pct in won:
        breaker.send_alert(
            day=reservation.day,
            pct=pct,
            settled_micros=settled_micros,
            reserved_micros=reserved_micros,
            ceiling_micros=reservation.ceiling_micros,
            settlement=settlement,
        )

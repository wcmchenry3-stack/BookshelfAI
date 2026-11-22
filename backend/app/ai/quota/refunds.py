"""Giving back a unit at most once (#590)."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import quota
from app.ai.quota.constants import (
    GIVE_BACK_DONE,
    UNIT_KEPT_FALLBACK_FAILED,
    UNIT_KEPT_GIVE_BACK_FAILED,
    UNIT_KEPT_LEASE_LOST_UNACKNOWLEDGED,
    UNIT_LEASE_LOST,
)
from app.ai.quota.types import Reservation, _Unit


async def _return_unit(
    db: AsyncSession, reservation: Reservation, unit: _Unit, *, taken: bool
) -> None:
    """Give back one unit at most once (#590), bounded and retried like any
    give-back (:func:`_give_back`).

    The gate is :func:`give_back_unit`: the flag on the request's lease row
    is cleared in the same commit as the decrement, so a retry after a lost
    acknowledgement finds it clear and gives nothing back, and a unit whose
    take was never acknowledged is given back only if its flag shows it
    landed. That is why the refunds retry again (#567 had made them
    one-shot because nothing gated them).

    ``taken`` means this request knows the unit was taken (its take was
    acknowledged). If another request took over the expired lease, the
    flags belong to the new lease (the takeover resets them), so the gate
    cannot show anything: on the first attempt only, a known-taken unit is
    then given back once without the gate (:func:`refund_unit`, as before
    #590). A later attempt never does that, since the first may have
    committed. A unit whose take was not acknowledged and whose lease was
    taken over stays charged (fails toward charging)."""
    attempts = 0
    last_outcome: str | None = None
    fallback_done = False

    async def op() -> None:
        nonlocal attempts, last_outcome, fallback_done
        attempts += 1
        last_outcome = None
        outcome = await quota.give_back_unit(
            db,
            reservation.user_id,
            reservation.lease_id,
            unit.flag,
            unit.feature,
            unit.window_kind,
            unit.window_start,
        )
        last_outcome = outcome
        if outcome == UNIT_LEASE_LOST and taken and attempts == 1:
            await quota.refund_unit(
                db,
                reservation.user_id,
                unit.feature,
                unit.window_kind,
                unit.window_start,
            )
            fallback_done = True

    result = await quota._give_back(db, f"{unit.what} refund", op)
    # An accepted exception (#590), tracked: a unit left charged.
    if result != GIVE_BACK_DONE:
        quota.send_unit_kept_warning(unit, UNIT_KEPT_GIVE_BACK_FAILED)
    elif last_outcome == UNIT_LEASE_LOST and not taken:
        quota.send_unit_kept_warning(unit, UNIT_KEPT_LEASE_LOST_UNACKNOWLEDGED)
    elif last_outcome == UNIT_LEASE_LOST and not fallback_done:
        quota.send_unit_kept_warning(unit, UNIT_KEPT_FALLBACK_FAILED)


async def refund(db: AsyncSession, reservation: Reservation) -> None:
    """Give back the daily unit (provider unavailable or never reached), at
    most once (:func:`_return_unit`). Never raises; does not release the
    lease (call :func:`release`)."""
    await quota._return_unit(
        db, reservation, quota._daily_unit(reservation), taken=True
    )

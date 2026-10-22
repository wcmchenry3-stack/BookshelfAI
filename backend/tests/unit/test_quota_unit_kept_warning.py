"""The Sentry warning for a unit left charged (#590): one per unit per
request, with a fixed fingerprint and tags and no user data, and never a
change to the give-back's outcome."""

import datetime as dt
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai import quota
from app.ai.entitlements import Tier

DAY = dt.date(2026, 10, 21)


def _reservation() -> quota.Reservation:
    return quota.Reservation(
        user_id=uuid.uuid4(),
        feature=quota.SCAN_QUOTA_FEATURE,
        lease_id=uuid.uuid4(),
        day=DAY,
        month=DAY.replace(day=1),
        tier=Tier.FREE,
        allowance=quota.Allowance(5, 50),
    )


@pytest.fixture
def env(monkeypatch):
    db = MagicMock()
    db.rollback = AsyncMock()
    warnings = MagicMock()
    monkeypatch.setattr(quota.alerts, "capture_service_warning", warnings)
    refunds: list = []

    async def refund_unit(*args):
        refunds.append(args)

    monkeypatch.setattr(quota, "refund_unit", refund_unit)
    return db, warnings, refunds


def _gate(monkeypatch, *outcomes):
    """give_back_unit returns (or raises) each of ``outcomes`` in turn."""
    it = iter(outcomes)

    async def give_back_unit(*_args):
        o = next(it)
        if isinstance(o, Exception):
            raise o
        return o

    monkeypatch.setattr(quota, "give_back_unit", give_back_unit)


async def _run(env, *, taken, flag=quota.FLAG_DAILY):
    db = env[0]
    reservation = _reservation()
    unit = quota._units(reservation)[
        {quota.FLAG_DAILY: 0, quota.FLAG_MONTHLY: 1, quota.FLAG_ENHANCED: 2}[flag]
    ]
    await quota._return_unit(db, reservation, unit, taken=taken)
    return reservation


def _only_call(warnings, reservation, *, reason, unit):
    warnings.assert_called_once()
    kwargs = warnings.call_args.kwargs
    assert kwargs["fingerprint"] == ["ai-quota-unit-kept", reason]
    assert kwargs["tags"] == {"unit": unit, "reason": reason}
    blob = repr(warnings.call_args)
    assert str(reservation.user_id) not in blob
    assert str(reservation.lease_id) not in blob


@pytest.mark.parametrize(
    "flag, unit",
    [
        (quota.FLAG_DAILY, "daily"),
        (quota.FLAG_MONTHLY, "monthly"),
        (quota.FLAG_ENHANCED, "enhanced"),
    ],
)
async def test_lease_lost_and_not_acknowledged_warns_once(env, monkeypatch, flag, unit):
    _gate(monkeypatch, quota.UNIT_LEASE_LOST)
    reservation = await _run(env, taken=False, flag=flag)
    _only_call(env[1], reservation, reason="lease_lost_unacknowledged", unit=unit)
    assert env[2] == []


async def test_a_failing_fallback_warns_once(env, monkeypatch):
    async def failing(*_args):
        raise OSError("down")

    monkeypatch.setattr(quota, "refund_unit", failing)
    # Attempt 1: lease lost, the fallback raises; attempt 2: lease lost, the
    # fallback is skipped (the first may have committed).
    _gate(monkeypatch, quota.UNIT_LEASE_LOST, quota.UNIT_LEASE_LOST)
    reservation = await _run(env, taken=True)
    _only_call(env[1], reservation, reason="fallback_failed", unit="daily")


async def test_every_attempt_raising_warns_once(env, monkeypatch):
    _gate(monkeypatch, OSError("a"), OSError("b"))
    reservation = await _run(env, taken=True, flag=quota.FLAG_MONTHLY)
    _only_call(env[1], reservation, reason="give_back_failed", unit="monthly")


async def test_a_timeout_warns_once(env, monkeypatch):
    async def never(*_args):
        import anyio

        await anyio.sleep(10)

    monkeypatch.setattr(quota, "give_back_unit", never)
    monkeypatch.setattr(quota, "CLEANUP_TIMEOUT_SECONDS", 0.05)
    reservation = await _run(env, taken=True)
    _only_call(env[1], reservation, reason="give_back_failed", unit="daily")


@pytest.mark.parametrize("outcome", [quota.UNIT_GIVEN_BACK, quota.UNIT_NOT_TAKEN])
async def test_the_normal_outcomes_send_nothing(env, monkeypatch, outcome):
    _gate(monkeypatch, outcome)
    await _run(env, taken=True)
    env[1].assert_not_called()


async def test_a_successful_fallback_sends_nothing(env, monkeypatch):
    _gate(monkeypatch, quota.UNIT_LEASE_LOST)
    await _run(env, taken=True)
    env[1].assert_not_called()
    assert len(env[2]) == 1


async def test_a_retry_that_succeeds_sends_nothing(env, monkeypatch):
    _gate(monkeypatch, OSError("lost ack"), quota.UNIT_NOT_TAKEN)
    await _run(env, taken=True)
    env[1].assert_not_called()


async def test_a_failing_helper_does_not_break_the_give_back(env, monkeypatch):
    env[1].side_effect = RuntimeError("sentry down")
    _gate(monkeypatch, quota.UNIT_LEASE_LOST)
    await _run(env, taken=False)  # does not raise
    env[1].assert_called_once()

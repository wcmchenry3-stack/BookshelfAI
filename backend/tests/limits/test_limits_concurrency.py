"""Parallel requests against a limit, and lost commit acknowledgements (#584),
on real Postgres.

- N parallel attempts against a limit L never charge more than L, for each
  window (daily, monthly, the Premium enhanced monthly) and for the Free
  spend ceiling: separate sessions (connections), ``asyncio.gather``.
- A commit whose acknowledgement is lost (the commit lands, then the call
  raises, so the give-back retries) never charges or gives back twice. The
  pattern is ``test_retried_release_without_a_ledger_row_gives_back_once``
  (tests/integration/test_ai_breaker_db.py, #563).
"""

import asyncio

import pytest

from app.ai import quota
from app.ai.entitlements import Tier
from tests.limits import _harness as h
from tests.limits.test_limits_matrix import BASE

pytestmark = pytest.mark.integration

N = 20
L = 4


async def _scan_until_decided(db, user_id, *, enhanced) -> str:
    """One client: retries while another scan of the user holds the lease
    (as the app does), and returns how it ended."""
    for _ in range(1_000):
        got = await h.call_scan(db.factory, user_id, enhanced=enhanced)
        if got.code != "scan_in_progress":
            return "ok" if got.status == 200 else got.code
        await asyncio.sleep(0.005)
    return "gave up"


@pytest.mark.parametrize(
    "tier, enhanced, limit_key, refusal, window",
    [
        (Tier.FREE, False, "quota.free.daily", "daily_scan_limit", "day"),
        (Tier.FREE, False, "quota.free.monthly", "scan_limit_reached", "month"),
        (Tier.PREMIUM, False, "quota.premium.daily", "daily_scan_limit", "day"),
        (Tier.PREMIUM, True, "quota.premium.monthly", "scan_limit_reached", "month"),
        (
            Tier.PREMIUM,
            True,
            "quota.premium.enhanced_monthly",
            "enhanced_limit_reached",
            "enhanced",
        ),
    ],
    ids=[
        "free-daily",
        "free-monthly",
        "premium-daily",
        "premium-enhanced-shared-monthly",
        "premium-enhanced-monthly",
    ],
)
async def test_parallel_scans_never_charge_more_than_the_limit(
    limits_db, downstream, tier, enhanced, limit_key, refusal, window
):
    db = limits_db
    day, user_id = db.make_day(), await db.make_user()
    rows = {
        **BASE,
        "quota.free.daily": 1_000,
        "quota.free.monthly": 1_000,
        "quota.premium.daily": 1_000,
        "quota.premium.monthly": 1_000,
        "quota.premium.enhanced_monthly": 1_000,
        limit_key: L,
    }
    h.use_settings(rows)
    provider = h.FakeProvider("books", delay=0.01)
    with h.Clock(h.at(day)).patch(), h.as_tier(tier), provider.patch():
        outcomes = await asyncio.gather(
            *(_scan_until_decided(db, user_id, enhanced=enhanced) for _ in range(N))
        )

    assert sorted(outcomes) == sorted(["ok"] * L + [refusal] * (N - L))
    assert provider.calls == L
    assert provider.max_in_flight == 1
    after = await db.counters(user_id, day)
    assert (after.daily, after.monthly) == (L, L)
    assert after.enhanced == (L if enhanced else 0)
    assert await db.lease(user_id) is None


async def test_parallel_reservations_on_separate_connections_never_pass_the_limit(
    limits_db,
):
    """The reservation itself, without the lease that serialises one user:
    N concurrent upserts on N connections for one window, limit L."""
    db = limits_db
    day, user_id = db.make_day(), await db.make_user()

    async def one(kind, start):
        async with db.factory() as session:
            return await quota.reserve_unit(session, user_id, h.SCAN, kind, start, L)

    for kind, start in (("day", day), ("month", quota.month_start(day))):
        results = await asyncio.gather(*(one(kind, start) for _ in range(N)))
        assert sorted(r for r in results if r is not None) == list(range(1, L + 1))
        assert await db.units(user_id, feature=h.SCAN, kind=kind, start=start) == L


async def test_parallel_free_users_never_pass_the_spend_ceiling(limits_db, downstream):
    """N Free users scan at once with room for L estimates under the ceiling:
    exactly L reach the provider."""
    db = limits_db
    day = db.make_day()
    users = [await db.make_user() for _ in range(N)]
    est = h.estimate(False)
    h.use_settings({**BASE, "breaker.free_daily_ceiling_micros": L * est})
    gate = asyncio.Event()

    async def hold():
        await gate.wait()  # keep every reservation until all have tried

    provider = h.FakeProvider("books", on_call=hold)

    async def release_when_refused(tasks):
        while sum(t.done() for t in tasks) < N - L:
            await asyncio.sleep(0.01)
        gate.set()

    with h.Clock(h.at(day)).patch(), h.as_tier(Tier.FREE), provider.patch():
        tasks = [
            asyncio.create_task(h.call_scan(db.factory, u, enhanced=False))
            for u in users
        ]
        await asyncio.wait_for(release_when_refused(tasks), 30)
        outcomes = [t.code for t in await asyncio.gather(*tasks)]

    assert sorted(outcomes, key=str) == sorted(
        [None] * L + ["ai_budget_reached"] * (N - L), key=str
    )
    assert provider.calls == L
    reserved, _ = await db.spend(day)
    assert reserved == 0
    units = sorted([(await db.counters(u, day)).daily for u in users])
    assert units == [0] * (N - L) + [1] * L


# ---------------------------------------------------------------------------
# Lost commit acknowledgements
# ---------------------------------------------------------------------------


def _ack_lost_once(real, calls: list):
    """``real`` commits, then the first call raises as if the commit's
    acknowledgement was lost (the commit is durable)."""

    async def committed_then_raised(*args, **kwargs):
        calls.append(args)
        result = await real(*args, **kwargs)
        if len(calls) == 1:
            raise OSError("commit acknowledgement lost")
        return result

    return committed_then_raised


async def test_lost_ack_on_the_settle_gives_back_the_enhanced_unit_once(
    limits_db, downstream, monkeypatch
):
    """A zero-book enhanced scan below the threshold: the settle (ledger row,
    spend, charge decision, lease delete) commits, the ack is lost and the
    settle is retried. The enhanced unit is given back once, not twice; the
    spend is settled once; one zero-result row."""
    db = limits_db
    day, user_id = db.make_day(), await db.make_user()
    month = quota.month_start(day)
    await db.seed_units(user_id, feature=h.ENHANCED, kind="month", start=month, units=2)
    h.use_settings(BASE)
    calls: list = []
    monkeypatch.setattr(
        quota, "release_lease", _ack_lost_once(quota.release_lease, calls)
    )
    before = await db.counters(user_id, day)
    with (
        h.Clock(h.at(day)).patch(),
        h.as_tier(Tier.PREMIUM),
        h.FakeProvider("no_books").patch(),
    ):
        got = await h.call_scan(db.factory, user_id, enhanced=True)
    assert got.status == 200
    assert len(calls) == 2  # retried
    assert (await db.counters(user_id, day)).delta(before) == {
        "daily": 1,
        "monthly": 1,
        "enhanced": 0,
        "global": 1,
        "zero_results": 1,
    }
    assert await db.spend(day) == (0, h.cost(True, BASE))
    assert await db.lease(user_id) is None


async def test_lost_ack_on_a_refusal_release_gives_back_the_estimate_once(
    limits_db, downstream, monkeypatch
):
    """A refusal after the cost reservation (daily limit): the release (spend
    give-back and lease delete) commits, the ack is lost and it is retried.
    The estimate is given back once: another request's reservation stays."""
    db = limits_db
    day, user_id = db.make_day(), await db.make_user()
    rows = {**BASE, "quota.free.daily": 1}
    await db.seed_units(user_id, feature=h.SCAN, kind="day", start=day, units=1)
    other_est = 7_000  # another request's estimate, in flight
    async with db.factory() as session:
        from app.ai import breaker

        assert await breaker.reserve(session, day, other_est, 10**9, enforce=True)
    h.use_settings(rows)
    calls: list = []
    monkeypatch.setattr(
        quota, "release_lease", _ack_lost_once(quota.release_lease, calls)
    )
    with (
        h.Clock(h.at(day)).patch(),
        h.as_tier(Tier.FREE),
        h.FakeProvider("books").patch() as fake,
    ):
        got = await h.call_scan(db.factory, user_id, enhanced=False)
    assert got.code == "daily_scan_limit"
    assert fake.calls == 0
    assert len(calls) == 2
    assert await db.spend(day) == (other_est, 0)
    assert await db.units(user_id, feature=h.SCAN, kind="day", start=day) == 1


async def test_lost_ack_on_a_reservation_never_charges_twice(
    limits_db, downstream, monkeypatch
):
    """The daily reservation commits and its ack is lost: the request fails
    (500) without reaching the provider. The unit it committed is kept (fails
    toward charging; nothing shows the commit landed) but never taken twice,
    the monthly unit is never taken, and the lease and estimate are given
    back."""
    db = limits_db
    day, user_id = db.make_day(), await db.make_user()
    h.use_settings(BASE)
    calls: list = []
    monkeypatch.setattr(
        quota, "reserve_unit", _ack_lost_once(quota.reserve_unit, calls)
    )
    before = await db.counters(user_id, day)
    with (
        h.Clock(h.at(day)).patch(),
        h.as_tier(Tier.FREE),
        h.FakeProvider("books").patch() as fake,
    ):
        got = await h.call_scan(db.factory, user_id, enhanced=False)
    assert got.status == 500
    assert fake.calls == 0
    delta = (await db.counters(user_id, day)).delta(before)
    assert delta["daily"] == 1 and delta["monthly"] == 0
    assert await db.spend(day) == (0, 0)
    assert await db.lease(user_id) is None


@pytest.mark.parametrize(
    "lost_feature, lost_kind",
    [
        pytest.param(h.SCAN, "day", id="daily"),
        pytest.param(h.SCAN, "month", id="monthly"),
        pytest.param(h.ENHANCED, "month", id="enhanced"),
    ],
)
async def test_lost_ack_on_a_refund_gives_back_once(
    limits_db, downstream, monkeypatch, lost_feature, lost_kind
):
    """An enhanced scan finds the provider unavailable, so its units are
    refunded. One refund commits and its ack is lost, so the give-back may
    retry. Every unit must come back exactly once: the user had 2 of each
    before the scan, so they have 2 after it, not 1."""
    db = limits_db
    day, user_id = db.make_day(), await db.make_user()
    month = quota.month_start(day)
    for feature, kind, start in (
        (h.SCAN, "day", day),
        (h.SCAN, "month", month),
        (h.ENHANCED, "month", month),
    ):
        await db.seed_units(user_id, feature=feature, kind=kind, start=start, units=2)
    h.use_settings(BASE)
    real = quota.refund_unit
    lost: list = []

    async def committed_then_raised(session, uid, feature, kind, start):
        await real(session, uid, feature, kind, start)
        if (feature, kind) == (lost_feature, lost_kind) and not lost:
            lost.append(1)
            raise OSError("commit acknowledgement lost")

    monkeypatch.setattr(quota, "refund_unit", committed_then_raised)
    before = await db.counters(user_id, day)
    with (
        h.Clock(h.at(day)).patch(),
        h.as_tier(Tier.PREMIUM),
        h.FakeProvider("unavailable").patch(),
    ):
        got = await h.call_scan(db.factory, user_id, enhanced=True)
    assert got.code == "scan_unavailable"
    assert lost == [1]
    assert (await db.counters(user_id, day)).delta(before) == {
        "daily": 0,
        "monthly": 0,
        "enhanced": 0,
        "global": 0,
        "zero_results": 0,
    }

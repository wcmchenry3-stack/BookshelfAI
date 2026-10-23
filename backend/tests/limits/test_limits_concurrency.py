"""Parallel requests against a limit, and lost commit acknowledgements (#584),
on real Postgres.

- N parallel attempts against a limit L never charge more than L, for the
  daily limit (Free, Premium, and a Premium enhanced scan, which uses the
  same daily unit) and for the Free spend ceiling: separate sessions
  (connections), ``asyncio.gather``. There is no monthly window (#582); no
  month row is ever written.
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
    "tier, enhanced, limit_key, refusal",
    [
        (Tier.FREE, False, "quota.free.found_daily", "daily_scan_limit"),
        (Tier.PREMIUM, False, "quota.premium.found_daily", "daily_scan_limit"),
        (Tier.PREMIUM, True, "quota.premium.found_daily", "daily_scan_limit"),
    ],
    ids=["free-daily", "premium-daily", "premium-enhanced-daily"],
)
async def test_parallel_scans_never_charge_more_than_the_limit(
    limits_db, downstream, tier, enhanced, limit_key, refusal
):
    db = limits_db
    day, user_id = db.make_day(), await db.make_user()
    rows = {
        **BASE,
        "quota.free.found_daily": 1_000,
        "quota.premium.found_daily": 1_000,
        limit_key: L,
        # #586: every retry on scan_in_progress is a /scan request; this test
        # is about the daily unit, so the request limit is raised.
        "scan.requests_per_user_daily": 10_000,
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
    assert (after.daily, after.monthly, after.enhanced) == (L, 0, 0)
    assert await db.lease(user_id) is None


async def test_parallel_reservations_on_separate_connections_never_pass_the_limit(
    limits_db,
):
    """The reservation itself, without the lease that serialises one user:
    N concurrent upserts on N connections for the day window, limit L."""
    db = limits_db
    day, user_id = db.make_day(), await db.make_user()

    async def one():
        async with db.factory() as session:
            return await quota.reserve_unit(session, user_id, h.SCAN, "day", day, L)

    results = await asyncio.gather(*(one() for _ in range(N)))
    assert sorted(r for r in results if r is not None) == list(range(1, L + 1))
    assert await db.units(user_id, feature=h.SCAN, kind="day", start=day) == L


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
        try:
            await asyncio.wait_for(release_when_refused(tasks), 10)
            outcomes = [t.code for t in await asyncio.gather(*tasks)]
        finally:
            # On a regression (too many scans let through) do not leave the
            # tasks parked at the gate.
            gate.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

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


async def test_lost_ack_on_the_settle_records_the_zero_result_once(
    limits_db, downstream, monkeypatch
):
    """A zero-book enhanced scan below the threshold: the settle (ledger row,
    spend, zero-result record, lease delete) commits, the ack is lost and the
    settle is retried. The spend is settled once; one zero-result row; the
    daily unit stays charged (#582: there is no enhanced unit to give back)
    and the legacy month rows are untouched."""
    db = limits_db
    day, user_id = db.make_day(), await db.make_user()
    await _seed_two_of_each(db, user_id, day)
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
        "monthly": 0,
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
    rows = {**BASE, "quota.free.found_daily": 1}
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


async def _seed_two_of_each(db, user_id, day) -> None:
    """2 units in the day window, so a unit given back twice shows as -1, and
    2 in each legacy month row (#582), which no scan may move."""
    month = day.replace(day=1)
    for feature, kind, start in (
        (h.SCAN, "day", day),
        (h.SCAN, "month", month),
        (h.ENHANCED, "month", month),
    ):
        await db.seed_units(user_id, feature=feature, kind=kind, start=start, units=2)


@pytest.mark.parametrize("give_back_ack_lost", [False, True], ids=["", "gb-lost"])
@pytest.mark.parametrize(
    "tier, enhanced",
    [
        pytest.param(Tier.FREE, False, id="free-standard-daily"),
        pytest.param(Tier.PREMIUM, True, id="premium-enhanced-daily"),
    ],
)
async def test_lost_ack_on_a_reservation_gives_the_unit_back_once(
    limits_db, downstream, monkeypatch, tier, enhanced, give_back_ack_lost
):
    """The daily unit's reservation (since #582 the only unit, for a standard
    and for an enhanced scan) commits and its ack is lost (#590), for a Free
    standard scan and a Premium enhanced one: the request fails (500) without
    reaching the provider, and every counter ends where it started. The unit
    is given back (its flag on the lease row shows it landed), and nothing
    is given back twice, even when the give-back's own ack is lost too and it
    is retried. The lease and the estimate are given back."""
    lost_flag = quota.FLAG_DAILY
    db = limits_db
    day, user_id = db.make_day(), await db.make_user()
    await _seed_two_of_each(db, user_id, day)
    h.use_settings(BASE)
    takes: list = []
    real_take = quota.take_unit

    async def take_ack_lost(session, uid, lease_id, flag, *rest):
        takes.append(flag)
        used = await real_take(session, uid, lease_id, flag, *rest)  # committed
        if flag == lost_flag:
            raise OSError("commit acknowledgement lost")
        return used

    give_backs: list = []
    real_give_back = quota.give_back_unit

    async def give_back(session, uid, lease_id, flag, *rest):
        outcome = await real_give_back(session, uid, lease_id, flag, *rest)
        if flag == lost_flag:
            give_backs.append(outcome)
            if give_back_ack_lost and len(give_backs) == 1:
                raise OSError("commit acknowledgement lost")
        return outcome

    monkeypatch.setattr(quota, "take_unit", take_ack_lost)
    monkeypatch.setattr(quota, "give_back_unit", give_back)
    before = await db.counters(user_id, day)
    with (
        h.Clock(h.at(day)).patch(),
        h.as_tier(tier),
        h.FakeProvider("books").patch() as fake,
    ):
        got = await h.call_scan(db.factory, user_id, enhanced=enhanced)
    assert got.status == 500
    assert fake.calls == 0
    assert takes == [quota.FLAG_DAILY]  # never taken twice; nothing else taken
    if give_back_ack_lost:
        # Retried; the gate gives nothing back the second time.
        assert give_backs == [quota.UNIT_GIVEN_BACK, quota.UNIT_NOT_TAKEN]
    else:
        assert give_backs == [quota.UNIT_GIVEN_BACK]
    assert (await db.counters(user_id, day)).delta(before) == {
        "daily": 0,
        "monthly": 0,
        "enhanced": 0,
        "global": 0,
        "zero_results": 0,
    }
    assert await db.spend(day) == (0, 0)
    assert await db.lease(user_id) is None


async def test_lost_ack_on_a_reservation_after_a_lease_takeover_stays_charged(
    limits_db, downstream, monkeypatch
):
    """The daily reservation commits, its ack is lost, and meanwhile the
    stalled request's lease expired and another request took it over (#590).
    The flags now belong to the new lease (the takeover reset them), so the
    old request's gated give-back finds nothing: its daily unit, which it
    never saw land, stays charged (fails toward charging). The new holder's
    lease and flags are untouched. (Before #582 this was the monthly unit;
    the daily one is now the only unit.)"""
    db = limits_db
    day, user_id = db.make_day(), await db.make_user()
    await _seed_two_of_each(db, user_id, day)
    h.use_settings(BASE)
    real_take = quota.take_unit
    new_holder: list = []

    async def take_then_taken_over(session, uid, lease_id, flag, *rest):
        used = await real_take(session, uid, lease_id, flag, *rest)  # committed
        if flag == quota.FLAG_DAILY:
            await db.expire_lease(uid)
            new_holder.append(await db.hold_lease(uid))
            raise OSError("commit acknowledgement lost")
        return used

    monkeypatch.setattr(quota, "take_unit", take_then_taken_over)
    before = await db.counters(user_id, day)
    with (
        h.Clock(h.at(day)).patch(),
        h.as_tier(Tier.PREMIUM),
        h.FakeProvider("books").patch() as fake,
    ):
        got = await h.call_scan(db.factory, user_id, enhanced=True)
    assert got.status == 500
    assert fake.calls == 0
    assert (await db.counters(user_id, day)).delta(before) == {
        "daily": 1,
        "monthly": 0,
        "enhanced": 0,
        "global": 1,
        "zero_results": 0,
    }
    assert await db.lease(user_id) == new_holder[0]
    assert await db.lease_flags(user_id) == set()


@pytest.mark.parametrize("enhanced", [False, True], ids=["standard", "enhanced"])
async def test_lost_ack_on_a_refund_gives_back_once(
    limits_db, downstream, monkeypatch, enhanced
):
    """A scan finds the provider unavailable, so its daily unit (#582: the
    only unit) is refunded. The refund commits and its ack is lost, so the
    give-back retries. The unit must come back exactly once: the user had 2
    before the scan, so they have 2 after it, not 1."""
    db = limits_db
    day, user_id = db.make_day(), await db.make_user()
    await _seed_two_of_each(db, user_id, day)
    lost_feature, lost_kind = h.SCAN, "day"
    h.use_settings(BASE)
    real = quota.give_back_unit
    lost: list = []

    async def committed_then_raised(session, uid, lease_id, flag, feature, kind, start):
        outcome = await real(session, uid, lease_id, flag, feature, kind, start)
        if (feature, kind) == (lost_feature, lost_kind) and not lost:
            lost.append(1)
            raise OSError("commit acknowledgement lost")
        return outcome

    monkeypatch.setattr(quota, "give_back_unit", committed_then_raised)
    before = await db.counters(user_id, day)
    with (
        h.Clock(h.at(day)).patch(),
        h.as_tier(Tier.PREMIUM),
        h.FakeProvider("unavailable").patch(),
    ):
        got = await h.call_scan(db.factory, user_id, enhanced=enhanced)
    assert got.code == "scan_unavailable"
    assert lost == [1]
    assert (await db.counters(user_id, day)).delta(before) == {
        "daily": 0,
        "monthly": 0,
        "enhanced": 0,
        "global": 0,
        "zero_results": 0,
    }


# ---------------------------------------------------------------------------
# The takeover fallback is at most once (#590)
# ---------------------------------------------------------------------------


async def _taken_reservation(db, user_id, day) -> quota.Reservation:
    """A standard scan's reservation with its daily unit taken
    (acknowledged) on top of 2 already used, so a unit given back twice
    shows as -1 and one never given back as +1."""
    await _seed_two_of_each(db, user_id, day)
    async with db.factory() as session:
        lease_id = await quota.acquire_lease(session, user_id, 600)
        assert lease_id is not None
        await quota.take_unit(
            session, user_id, lease_id, quota.FLAG_DAILY, h.SCAN, "day", day, 10
        )
    return quota.Reservation(
        user_id=user_id,
        feature=h.SCAN,
        lease_id=lease_id,
        day=day,
        tier=Tier.FREE,
        allowance=quota.Allowance(10),
    )


async def _used(db, user_id, day) -> tuple[int, int]:
    """The standard scan's daily units used, and the legacy month row (#582:
    seeded at 2 and never moved)."""
    return (
        await db.units(user_id, feature=h.SCAN, kind="day", start=day),
        await db.units(user_id, feature=h.SCAN, kind="month", start=day.replace(day=1)),
    )


async def test_a_give_back_whose_ack_is_lost_before_a_takeover_is_not_repeated(
    limits_db, monkeypatch
):
    """The gated give-back of the daily unit commits (flag cleared, unit
    returned) but its ack is lost, and the lease is then taken over before
    the retry. The retry sees ``lease_lost`` and must not run the ungated
    ``refund_unit``: the count ends where it started (2), not one lower."""
    db = limits_db
    day, user_id = db.make_day(), await db.make_user()
    reservation = await _taken_reservation(db, user_id, day)
    assert await _used(db, user_id, day) == (3, 2)
    real = quota.give_back_unit
    seen: list = []

    async def committed_taken_over_then_raised(session, uid, lease_id, flag, *rest):
        outcome = await real(session, uid, lease_id, flag, *rest)  # committed
        if flag == quota.FLAG_DAILY and not seen:
            seen.append(outcome)
            await db.expire_lease(uid)
            await db.hold_lease(uid)
            raise OSError("commit acknowledgement lost")
        return outcome

    monkeypatch.setattr(quota, "give_back_unit", committed_taken_over_then_raised)
    async with db.factory() as session:
        await quota.refund(session, reservation)
    assert seen == [quota.UNIT_GIVEN_BACK]
    assert await _used(db, user_id, day) == (2, 2)


async def test_an_ungated_refund_whose_ack_is_lost_is_not_repeated(
    limits_db, monkeypatch
):
    """The lease was taken over, so the gated give-back finds ``lease_lost``
    and the known-taken unit is given back once by the ungated
    ``refund_unit``. That commits but its ack is lost: the retry must not run
    it again (a later attempt may follow one that committed). Exactly one
    decrement per unit."""
    db = limits_db
    day, user_id = db.make_day(), await db.make_user()
    reservation = await _taken_reservation(db, user_id, day)
    await db.expire_lease(user_id)
    await db.hold_lease(user_id)
    real = quota.refund_unit
    calls: list = []

    async def committed_then_raised(session, uid, feature, kind, start):
        calls.append(kind)
        await real(session, uid, feature, kind, start)  # committed
        if kind == "day" and calls.count("day") == 1:
            raise OSError("commit acknowledgement lost")

    monkeypatch.setattr(quota, "refund_unit", committed_then_raised)
    async with db.factory() as session:
        await quota.refund(session, reservation)
    assert calls == ["day"]  # not run again
    assert await _used(db, user_id, day) == (2, 2)


async def test_a_refund_after_a_takeover_gives_each_known_taken_unit_back_once(
    limits_db, monkeypatch
):
    """The provider was unavailable (``refund``) and the lease was taken over
    meanwhile, so the flags are the new holder's and the gate shows nothing.
    The units this request knows it took are given back once each by the
    fallback, and the new holder's lease and flags are untouched."""
    db = limits_db
    day, user_id = db.make_day(), await db.make_user()
    reservation = await _taken_reservation(db, user_id, day)
    await db.expire_lease(user_id)
    new_holder = await db.hold_lease(user_id)
    calls: list = []
    real = quota.refund_unit

    async def counting(session, uid, feature, kind, start):
        calls.append(kind)
        await real(session, uid, feature, kind, start)

    monkeypatch.setattr(quota, "refund_unit", counting)
    async with db.factory() as session:
        await quota.refund(session, reservation)
    assert calls == ["day"]
    assert await _used(db, user_id, day) == (2, 2)
    assert await db.lease(user_id) == new_holder
    assert await db.lease_flags(user_id) == set()

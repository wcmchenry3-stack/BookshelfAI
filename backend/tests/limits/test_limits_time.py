"""Time and the scan limits (#584), with the request clock (``quota.utc_now``)
controlled and real Postgres:

- the daily counter resets at UTC midnight;
- a scan reserved before midnight and settled (or refunded) after it is
  charged to one day only: the day it was reserved on;
- a month boundary changes nothing (#582: there is no monthly limit): a
  user at the daily limit on the last day of a month is refused for the
  daily reason only, scans again one second after midnight, and no month
  row is read or written, including across a year end;
- heavy use over many days (40 days at the Free daily limit, and Premium
  enhanced scans at the daily limit past the old monthly and enhanced
  monthly allowances) is never refused for a monthly reason, while the
  daily limit still holds every day.
"""

import random
from datetime import date, timedelta

import pytest

from app.ai.entitlements import Tier
from tests.limits import _harness as h
from tests.limits.test_limits_matrix import BASE

pytestmark = pytest.mark.integration


def _far_month_end(db: h.LimitsDb, month: int) -> date:
    """The last day of ``month`` in a far-past year no other test uses."""
    while True:
        year = random.randrange(1800, 1899)
        first_of_next = date(year + month // 12, month % 12 + 1, 1)
        last = first_of_next - timedelta(days=1)
        if last not in db.days:
            return db.claim_day(last)


@pytest.fixture
def free():
    with h.as_tier(Tier.FREE):
        yield


async def test_the_daily_counter_resets_at_utc_midnight(limits_db, downstream, free):
    db = limits_db
    day, user_id = db.make_day(), await db.make_user()
    h.use_settings({**BASE, "quota.free.daily": 2})
    clock = h.Clock(h.at(day, 23, 59, 58))
    with clock.patch(), h.FakeProvider("books").patch() as fake:
        for _ in range(2):
            assert (
                await h.call_scan(db.factory, user_id, enhanced=False)
            ).status == 200
        clock.now = h.at(day, 23, 59, 59)
        refused = await h.call_scan(db.factory, user_id, enhanced=False)
        assert refused.code == "daily_scan_limit"
        assert refused.headers == {"Retry-After": "1"}

        clock.now = h.at(day + timedelta(days=1), 0, 0, 0)
        assert (await h.call_scan(db.factory, user_id, enhanced=False)).status == 200
    assert fake.calls == 3
    assert await db.units(user_id, feature=h.SCAN, kind="day", start=day) == 2
    next_day = day + timedelta(days=1)
    assert await db.units(user_id, feature=h.SCAN, kind="day", start=next_day) == 1
    # No month row (#582).
    month = day.replace(day=1)
    assert await db.row(user_id, feature=h.SCAN, kind="month", start=month) is None


@pytest.mark.parametrize(
    "outcome, expect_units, expect_spend",
    [
        ("books", 1, "cost"),
        ("unavailable", 0, "none"),
        ("timeout", 0, "estimate"),
    ],
)
async def test_a_scan_across_midnight_is_charged_to_the_day_it_was_reserved(
    limits_db, downstream, free, outcome, expect_units, expect_spend
):
    """Reserved at 23:59:59, the provider answers (or fails) after midnight:
    the units, the refund and the spend all land on the first day; the next
    day has no row and no spend."""
    db = limits_db
    day, user_id = db.make_day(), await db.make_user()
    next_day = day + timedelta(days=1)
    h.use_settings(BASE)
    clock = h.Clock(h.at(day, 23, 59, 59))

    def cross_midnight():
        clock.now = h.at(next_day, 0, 0, 1)

    with clock.patch(), h.FakeProvider(outcome, on_call=cross_midnight).patch():
        await h.call_scan(db.factory, user_id, enhanced=False)

    month = day.replace(day=1)
    assert await db.units(user_id, feature=h.SCAN, kind="day", start=day) == (
        expect_units
    )
    assert await db.row(user_id, feature=h.SCAN, kind="month", start=month) is None
    assert await db.row(user_id, feature=h.SCAN, kind="day", start=next_day) is None
    settled = {"cost": h.cost(False, BASE), "estimate": h.estimate(False), "none": 0}
    assert await db.spend(day) == (0, settled[expect_spend])
    assert await db.spend(next_day) == (0, 0)
    assert await db.lease(user_id) is None


async def test_an_enhanced_scan_across_the_month_end_is_charged_to_its_day(
    limits_db, downstream
):
    """Reserved on the last second of a month, a zero-book enhanced scan is
    settled in the next month: its daily unit lands on the day it was
    reserved, the zero-result row is written, and no month row exists in
    either month (#582)."""
    db = limits_db
    last = _far_month_end(db, 3)
    user_id = await db.make_user()
    old, new = last.replace(day=1), last + timedelta(days=1)
    h.use_settings(BASE)
    clock = h.Clock(h.at(last, 23, 59, 59))

    def cross():
        clock.now = h.at(new, 0, 0, 1)

    with (
        clock.patch(),
        h.as_tier(Tier.PREMIUM),
        h.FakeProvider("no_books", on_call=cross).patch(),
    ):
        got = await h.call_scan(db.factory, user_id, enhanced=True)
    assert got.status == 200
    # The daily allowance left on the day it was reserved.
    assert got.response.enhanced_scans_remaining == BASE["quota.premium.daily"] - 1
    assert await db.units(user_id, feature=h.SCAN, kind="day", start=last) == 1
    assert await db.row(user_id, feature=h.SCAN, kind="day", start=new) is None
    for start in (old, new):
        for feature in (h.SCAN, h.ENHANCED):
            assert (
                await db.row(user_id, feature=feature, kind="month", start=start)
                is None
            )
    assert (await db.counters(user_id, last)).zero_results == 1


@pytest.mark.parametrize("month", [1, 2, 12], ids=["jan", "feb", "dec"])
async def test_a_month_boundary_changes_nothing(limits_db, downstream, free, month):
    """At the daily limit on the last day of a month, with a month row far
    over any old monthly limit (left by the previous release) and the
    retired monthly key at its old value: the refusal is the daily one
    (Retry-After to midnight, no ``period_end``); one second after midnight
    the scan is allowed and counted on the new day only. No month row is
    read, written or created, also across a year end (December)."""
    db = limits_db
    last = _far_month_end(db, month)
    first = last + timedelta(days=1)
    assert first.day == 1
    if month == 12:
        assert first == date(last.year + 1, 1, 1)
    user_id = await db.make_user()
    h.use_settings({**BASE, "quota.free.daily": 2, "quota.free.monthly": 2})
    old_month = last.replace(day=1)
    await db.seed_units(
        user_id, feature=h.SCAN, kind="month", start=old_month, units=10_000
    )
    await db.seed_units(user_id, feature=h.SCAN, kind="day", start=last, units=2)
    clock = h.Clock(h.at(last, 23, 59, 59))
    with clock.patch(), h.FakeProvider("books").patch() as fake:
        refused = await h.call_scan(db.factory, user_id, enhanced=False)
        assert (refused.status, refused.code) == (429, "daily_scan_limit")
        assert refused.headers == {"Retry-After": "1"}
        assert refused.body is None

        clock.now = h.at(first, 0, 0, 1)
        assert (await h.call_scan(db.factory, user_id, enhanced=False)).status == 200
    assert fake.calls == 1
    assert await db.units(user_id, feature=h.SCAN, kind="day", start=first) == 1
    assert await db.units(user_id, feature=h.SCAN, kind="day", start=last) == 2
    assert (
        await db.units(user_id, feature=h.SCAN, kind="month", start=old_month) == 10_000
    )
    assert await db.row(user_id, feature=h.SCAN, kind="month", start=first) is None


async def _days_at_the_daily_limit(
    db: h.LimitsDb, user_id, start: date, days: int, *, limit: int, enhanced: bool
) -> list[str]:
    """Scan ``limit`` times on each of ``days`` consecutive UTC days from
    ``start``, then once more. Returns every outcome that was not the
    expected one (empty when every day allowed exactly ``limit`` scans and
    refused the next with ``daily_scan_limit``)."""
    unexpected: list[str] = []
    clock = h.Clock(h.at(start))
    with clock.patch():
        for n in range(days):
            day = db.claim_day(start + timedelta(days=n))
            clock.now = h.at(day, 9)
            for i in range(limit):
                got = await h.call_scan(db.factory, user_id, enhanced=enhanced)
                if got.status != 200:
                    unexpected.append(f"{day} scan {i + 1}: {got.status} {got.code}")
            over = await h.call_scan(db.factory, user_id, enhanced=enhanced)
            if (over.status, over.code) != (429, "daily_scan_limit"):
                unexpected.append(f"{day} over the limit: {over.status} {over.code}")
    return unexpected


def _far_start_crossing_months(db: h.LimitsDb, days: int) -> date:
    """A far-past start day whose next ``days`` days cross at least one
    month end and are used by no other test."""
    while True:
        # From the 25th, 11 days always cross a month end and 40 days two.
        start = date(random.randrange(1800, 1899), random.randrange(1, 13), 25)
        span = [start + timedelta(days=n) for n in range(days)]
        if not set(span) & set(db.days):
            assert span[-1].replace(day=1) != start.replace(day=1)
            return start


async def test_40_days_at_the_free_daily_limit_are_never_refused_monthly(
    limits_db, downstream, free
):
    """#582: 40 consecutive days at the Free daily limit (200 scans, 40 x
    the old monthly allowance of 5, across two month ends): every scan up to
    the limit is allowed, every day's next scan is refused for the daily
    reason only, and no month row is written."""
    db = limits_db
    days, limit = 40, BASE["quota.free.daily"]
    start = _far_start_crossing_months(db, days)
    user_id = await db.make_user()
    h.use_settings(BASE)
    with h.FakeProvider("books").patch() as fake:
        unexpected = await _days_at_the_daily_limit(
            db, user_id, start, days, limit=limit, enhanced=False
        )
    assert unexpected == []
    assert fake.calls == days * limit
    for n in range(days):
        day = start + timedelta(days=n)
        assert await db.units(user_id, feature=h.SCAN, kind="day", start=day) == limit
    assert await db.month_rows(user_id) == 0


async def test_premium_enhanced_scans_past_the_old_monthly_allowances(
    limits_db, downstream
):
    """#582: a Premium user making enhanced scans at the daily limit (30) for
    11 days (330 scans: past the old Premium monthly allowance of 300 and the
    old enhanced monthly allowance of 20, across a month end) is never
    refused for a monthly reason; the daily limit holds every day."""
    db = limits_db
    days, limit = 11, BASE["quota.premium.daily"]
    start = _far_start_crossing_months(db, days)
    user_id = await db.make_user()
    h.use_settings(BASE)
    with h.as_tier(Tier.PREMIUM), h.FakeProvider("books").patch() as fake:
        unexpected = await _days_at_the_daily_limit(
            db, user_id, start, days, limit=limit, enhanced=True
        )
    assert unexpected == []
    assert fake.calls == days * limit
    assert await db.month_rows(user_id) == 0

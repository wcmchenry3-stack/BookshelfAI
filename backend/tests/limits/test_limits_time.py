"""Time and the scan limits (#584), with the request clock (``quota.utc_now``)
controlled and real Postgres:

- the daily counter resets at UTC midnight;
- a scan reserved before midnight and settled (or refunded) after it is
  charged to one day only: the day it was reserved on;
- the month boundary as today's code intends it (the monthly window goes away
  in #582; until then it resets on the 1st of the UTC month, including across
  a year end).
"""

import random
from datetime import date, timedelta

import pytest

from app.ai import quota
from app.ai.entitlements import Tier
from tests.limits import _harness as h
from tests.limits.test_limits_matrix import BASE

pytestmark = pytest.mark.integration


def _far_month_end(db: h.LimitsDb, month: int) -> date:
    """The last day of ``month`` in a far-past year no other test uses."""
    while True:
        year = random.randrange(1800, 1899)
        last = quota.next_month_start(date(year, month, 1)) - timedelta(days=1)
        if last not in db.days:
            return db.claim_day(last)


@pytest.fixture
def free():
    with h.as_tier(Tier.FREE):
        yield


async def test_the_daily_counter_resets_at_utc_midnight(limits_db, downstream, free):
    db = limits_db
    day, user_id = db.make_day(), await db.make_user()
    h.use_settings({**BASE, "quota.free.daily": 2, "quota.free.monthly": 100})
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
    # Same month: the monthly count carries on.
    month = quota.month_start(day)
    assert await db.units(user_id, feature=h.SCAN, kind="month", start=month) == 3


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

    month = quota.month_start(day)
    assert await db.units(user_id, feature=h.SCAN, kind="day", start=day) == (
        expect_units
    )
    assert await db.units(user_id, feature=h.SCAN, kind="month", start=month) == (
        expect_units
    )
    assert await db.row(user_id, feature=h.SCAN, kind="day", start=next_day) is None
    settled = {"cost": h.cost(False, BASE), "estimate": h.estimate(False), "none": 0}
    assert await db.spend(day) == (0, settled[expect_spend])
    assert await db.spend(next_day) == (0, 0)
    assert await db.lease(user_id) is None


async def test_an_enhanced_scan_across_the_month_end_is_charged_to_its_month(
    limits_db, downstream
):
    """Reserved on the last second of a month, a zero-book enhanced scan is
    settled in the next month: the shared and enhanced units, and the
    enhanced give-back, all use the month it was reserved in."""
    db = limits_db
    last = _far_month_end(db, 3)
    user_id = await db.make_user()
    old, new = quota.month_start(last), last + timedelta(days=1)
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
    assert (
        got.response.enhanced_scans_remaining == BASE["quota.premium.enhanced_monthly"]
    )
    assert await db.units(user_id, feature=h.SCAN, kind="month", start=old) == 1
    assert await db.row(user_id, feature=h.SCAN, kind="month", start=new) is None
    # Given back below the zero-result threshold, in the old month's row.
    assert await db.row(user_id, feature=h.ENHANCED, kind="month", start=old) == 0
    assert await db.row(user_id, feature=h.ENHANCED, kind="month", start=new) is None


@pytest.mark.parametrize("month", [1, 2, 12], ids=["jan", "feb", "dec"])
async def test_the_monthly_counter_resets_on_the_first_of_the_utc_month(
    limits_db, downstream, free, month
):
    """At the monthly limit on the last day of a month (and not at the daily
    one): refused with ``period_end`` = the 1st of the next month (the next
    year after December); one second after midnight the scan is allowed and
    counted in the new month."""
    db = limits_db
    last = _far_month_end(db, month)
    first = last + timedelta(days=1)
    assert first.day == 1
    user_id = await db.make_user()
    h.use_settings({**BASE, "quota.free.daily": 10, "quota.free.monthly": 2})
    await db.seed_units(
        user_id, feature=h.SCAN, kind="month", start=quota.month_start(last), units=2
    )
    clock = h.Clock(h.at(last, 23, 59, 59))
    with clock.patch(), h.FakeProvider("books").patch() as fake:
        refused = await h.call_scan(db.factory, user_id, enhanced=False)
        assert (refused.status, refused.code) == (402, "scan_limit_reached")
        assert refused.body["period_end"] == first.isoformat()
        if month == 12:
            assert first == date(last.year + 1, 1, 1)

        clock.now = h.at(first, 0, 0, 1)
        assert (await h.call_scan(db.factory, user_id, enhanced=False)).status == 200
    assert fake.calls == 1
    assert await db.units(user_id, feature=h.SCAN, kind="month", start=first) == 1
    assert (
        await db.units(
            user_id, feature=h.SCAN, kind="month", start=quota.month_start(last)
        )
        == 2
    )
    # The refused attempt took nothing on the last day.
    assert await db.units(user_id, feature=h.SCAN, kind="day", start=last) == 0

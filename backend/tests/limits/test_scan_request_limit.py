"""The per-user daily /scan request limit (#586, decision #573 point 3), on
real Postgres, beyond the matrix rows (``test_limits_matrix.py``):

- at the limit the request is refused before any other work: no Turnstile
  call, no normalisation, no fingerprint or scan-record lookup, no allowance
  step and no provider call;
- N parallel requests against a limit L never pass L;
- a change through the operator CLI takes effect without a restart, and
  the CLI enforces the bounds;
- another user's count is unaffected; a new UTC day starts from zero;
- the rows are purged with the other day rows.
"""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai import quota, scan_records
from app.ai.entitlements import Tier
from app.api import scan as scan_api
from app.core.config import settings
from tests.limits import _harness as h
from tests.limits.test_limits_matrix import BASE

pytestmark = pytest.mark.integration

CODE = "scan_requests_daily_limit"
N = 20
L = 4


async def _requests(db: h.LimitsDb, user_id, day) -> int:
    return await db.units(user_id, feature=h.REQUESTS, kind="day", start=day)


async def test_refused_at_the_limit_before_any_other_work(limits_db, downstream):
    db = limits_db
    day, user_id = db.make_day(), await db.make_user()
    await db.seed_units(user_id, feature=h.REQUESTS, kind="day", start=day, units=3)
    h.use_settings({**BASE, "scan.requests_per_user_daily": 3})
    provider = h.FakeProvider("books")
    turnstile = AsyncMock(return_value=True)
    sanitize = MagicMock(side_effect=scan_api.sanitize_image)
    fingerprint = MagicMock(side_effect=scan_records.fingerprint)
    lookup = AsyncMock(side_effect=scan_records.lookup)
    reserve = AsyncMock(side_effect=quota.reserve)
    with (
        h.Clock(h.at(day)).patch(),
        h.as_tier(Tier.FREE),
        provider.patch(),
        patch.object(settings, "turnstile_required", True),
        patch.object(scan_api, "_verify_turnstile", turnstile),
        patch.object(scan_api, "sanitize_image", sanitize),
        patch.object(scan_records, "fingerprint", fingerprint),
        patch.object(scan_records, "lookup", lookup),
        patch.object(quota, "reserve", reserve),
    ):
        got = await h.call_scan(db.factory, user_id, enhanced=False)

    assert (got.status, got.code) == (429, CODE)
    # Until the next UTC midnight (the clock is noon).
    assert got.headers == {"Retry-After": "43200"}
    assert turnstile.await_count == 0
    assert sanitize.call_count == 0
    assert fingerprint.call_count == 0
    assert lookup.await_count == 0
    assert reserve.await_count == 0
    assert provider.calls == provider.built == 0
    after = await db.counters(user_id, day)
    assert (after.requests, after.daily, after.global_) == (3, 0, 0)
    assert await db.lease(user_id) is None
    assert await db.spend(day) == (0, 0)


async def test_parallel_requests_never_pass_the_limit(limits_db, downstream):
    db = limits_db
    day, user_id = db.make_day(), await db.make_user()
    h.use_settings({**BASE, "scan.requests_per_user_daily": L})
    provider = h.FakeProvider("books", delay=0.01)
    with h.Clock(h.at(day)).patch(), h.as_tier(Tier.FREE), provider.patch():
        outcomes = await asyncio.gather(
            *(h.call_scan(db.factory, user_id, enhanced=False) for _ in range(N))
        )
    codes = [o.code for o in outcomes]
    # L requests got through the count (each then scanned or met the lease);
    # every other one was refused for the request limit.
    assert codes.count(CODE) == N - L
    assert all(c in (None, "scan_in_progress") for c in codes if c != CODE)
    assert provider.calls <= L
    assert await _requests(db, user_id, day) == L
    assert await db.lease(user_id) is None


async def test_parallel_counts_on_separate_connections_never_pass_the_limit(
    limits_db,
):
    db = limits_db
    day, user_id = db.make_day(), await db.make_user()
    ai = h.ai_settings.AISettings({"scan.requests_per_user_daily": L})

    async def one():
        async with db.factory() as session:
            try:
                return await quota.count_scan_request(
                    session, user_id, ai, now=h.at(day)
                )
            except quota.ScanRequestsDailyLimitReached:
                return None

    results = await asyncio.gather(*(one() for _ in range(N)))
    assert sorted(r for r in results if r is not None) == list(range(1, L + 1))
    assert await _requests(db, user_id, day) == L


async def test_another_users_count_is_unaffected(limits_db, downstream):
    db = limits_db
    day, user_id, other = db.make_day(), await db.make_user(), await db.make_user()
    await db.seed_units(user_id, feature=h.REQUESTS, kind="day", start=day, units=5)
    h.use_settings({**BASE, "scan.requests_per_user_daily": 5})
    with (
        h.Clock(h.at(day)).patch(),
        h.as_tier(Tier.FREE),
        h.FakeProvider("books").patch(),
    ):
        refused = await h.call_scan(db.factory, user_id, enhanced=False)
        allowed = await h.call_scan(db.factory, other, enhanced=False)
    assert refused.code == CODE
    assert allowed.status == 200
    assert await _requests(db, user_id, day) == 5
    assert await _requests(db, other, day) == 1


async def test_a_new_utc_day_starts_from_zero(limits_db, downstream):
    db = limits_db
    day, user_id = db.make_day(), await db.make_user()
    h.use_settings({**BASE, "scan.requests_per_user_daily": 1})
    with h.as_tier(Tier.FREE), h.FakeProvider("books").patch():
        with h.Clock(h.at(day, 23, 59, 59)).patch():
            first = await h.call_scan(db.factory, user_id, enhanced=False)
            late = await h.call_scan(db.factory, user_id, enhanced=False)
        with h.Clock(h.at(day + timedelta(days=1), 0, 0, 0)).patch():
            next_day = await h.call_scan(db.factory, user_id, enhanced=False)
    assert first.status == 200
    assert (late.code, late.headers) == (CODE, {"Retry-After": "1"})
    assert next_day.status == 200
    assert await _requests(db, user_id, day + timedelta(days=1)) == 1


async def test_request_rows_are_purged_with_the_other_day_rows(limits_db):
    db = limits_db
    day, user_id = db.make_day(), await db.make_user()
    await db.seed_units(user_id, feature=h.REQUESTS, kind="day", start=day, units=7)
    async with db.factory() as session:
        # The day after the 90-day retention ends for ``day``.
        await quota.purge_old_quota(
            session, today=day + timedelta(days=quota.DAILY_RETENTION_DAYS + 1)
        )
    assert await db.row(user_id, feature=h.REQUESTS, kind="day", start=day) is None


# ---------------------------------------------------------------------------
# The setting, through the operator CLI, with no restart
# ---------------------------------------------------------------------------


async def test_cli_change_applies_without_restart_and_bounds_hold(
    ai_settings_table, downstream
):
    db = ai_settings_table
    day, user_id = db.make_day(), await db.make_user()
    clock = h.SettingsClock()

    async def scan():
        with h.Clock(h.at(day)).patch():
            return await h.call_scan(db.factory, user_id, enhanced=False)

    with clock.patch(), h.as_tier(Tier.FREE), h.FakeProvider("books").patch():
        result = await h.cli("set", "scan.requests_per_user_daily", "1", "--by", "ops")
        assert result.code == 0, result.err
        assert (await scan()).status == 200
        assert (await scan()).code == CODE

        result = await h.cli("set", "scan.requests_per_user_daily", "3", "--by", "ops")
        assert result.code == 0, result.err
        # Inside the cache TTL this process still serves the old value.
        assert (await scan()).code == CODE
        clock.expire()
        # The next request reads the table: no restart, no cache reset.
        assert (await scan()).status == 200
        assert (await scan()).status == 200
        assert (await scan()).code == CODE

        for bad in ("0", "10001", "-1", "true", "2.5"):
            rejected = await h.cli("set", "scan.requests_per_user_daily", bad)
            assert rejected.code != 0
        row = await h.stored_setting(db.factory, "scan.requests_per_user_daily")
        assert (row.value, row.updated_by) == (3, "ops")

    assert await _requests(db, user_id, day) == 3

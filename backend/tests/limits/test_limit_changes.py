"""Changing a scan limit in ``ai_settings`` (#584), on the real table with the
operator CLI (``python -m app.ai.settings set <key> <value> --by <who>``):

- the next request after the cache TTL obeys the new value, with no restart
  and no cache reset (the settings clock is moved past the TTL, as time
  passing in a running process would);
- raising a limit lets a refused user scan again the same day;
- lowering a limit below today's usage refuses the next scan, and no counter
  goes negative, even when a scan admitted before the change is refunded;
- out-of-bounds values are rejected and change nothing;
- the retired monthly keys (#582) are unknown to the CLI, and a row left by
  the previous release never limits anything;
- every change records who made it.
"""

from datetime import timedelta

import pytest

from app.ai import settings as ai_settings
from app.ai.entitlements import Tier
from app.api import scan as scan_api
from app.models.ai_settings import AISetting
from app.models.user import User
from tests.limits import _harness as h

pytestmark = pytest.mark.integration


async def _set(key: str, value: str, by: str = "ops") -> None:
    result = await h.cli("set", key, value, "--by", by)
    assert result.code == 0, result.err
    assert result.out.startswith(f"{key} = ")


async def _scan(db: h.LimitsDb, user_id, day, *, enhanced=False) -> h.ScanOutcome:
    with h.Clock(h.at(day)).patch():
        return await h.call_scan(db.factory, user_id, enhanced=enhanced)


async def _usage(db: h.LimitsDb, user_id, day):
    with h.Clock(h.at(day)).patch():
        async with db.factory() as session:
            user = await session.get(User, user_id)
            return await scan_api.scan_usage.__wrapped__(
                request=None, current_user=user, db=session
            )


@pytest.fixture
def settings_clock():
    clock = h.SettingsClock()
    with clock.patch():
        yield clock


@pytest.fixture
def provider():
    fake = h.FakeProvider("books")
    with fake.patch(), h.as_tier(Tier.FREE):
        yield fake


async def test_next_request_obeys_a_change_after_the_cache_ttl_without_restart(
    ai_settings_table, settings_clock, provider, downstream
):
    db = ai_settings_table
    day, user_id = db.make_day(), await db.make_user()
    await _set("quota.free.daily", "1")

    assert (await _scan(db, user_id, day)).status == 200
    assert (await _scan(db, user_id, day)).code == "daily_scan_limit"

    await _set("quota.free.daily", "2")
    # Inside the TTL this process still serves the value it read.
    assert (await _scan(db, user_id, day)).code == "daily_scan_limit"
    settings_clock.expire()
    # The next request reads the table: the new limit, same process.
    assert (await _scan(db, user_id, day)).status == 200
    assert provider.calls == 2
    assert await db.units(user_id, feature=h.SCAN, kind="day", start=day) == 2


async def test_raising_a_limit_lets_a_refused_user_scan_again_the_same_day(
    ai_settings_table, settings_clock, provider, downstream
):
    db = ai_settings_table
    day, user_id = db.make_day(), await db.make_user()
    await _set("quota.free.daily", "2")
    for _ in range(2):
        assert (await _scan(db, user_id, day)).status == 200
    refused = await _scan(db, user_id, day)
    assert (refused.status, refused.code) == (429, "daily_scan_limit")

    await _set("quota.free.daily", "3")
    settings_clock.expire()
    # The daily window has room now, and there is no monthly one (#582).
    assert (await _scan(db, user_id, day)).status == 200
    assert (await _scan(db, user_id, day)).code == "daily_scan_limit"
    assert provider.calls == 3
    usage = await _usage(db, user_id, day)
    assert (usage.used_today, usage.daily_limit) == (3, 3)
    assert usage.monthly_limit is None


async def test_lowering_a_limit_below_todays_usage_refuses_the_next_scan(
    ai_settings_table, settings_clock, provider, downstream
):
    db = ai_settings_table
    day, user_id = db.make_day(), await db.make_user()
    key, code, status = "quota.free.daily", "daily_scan_limit", 429
    await _set(key, "10")
    for _ in range(3):
        assert (await _scan(db, user_id, day)).status == 200

    await _set(key, "1")
    settings_clock.expire()
    refused = await _scan(db, user_id, day)
    assert (refused.status, refused.code) == (status, code)

    await _set(key, "0")
    settings_clock.expire()
    assert (await _scan(db, user_id, day)).code == code

    assert provider.calls == 3
    # Unchanged by the refusals: nothing taken, nothing given back twice.
    assert await db.units(user_id, feature=h.SCAN, kind="day", start=day) == 3
    # No month row is written (#582).
    month = day.replace(day=1)
    assert await db.row(user_id, feature=h.SCAN, kind="month", start=month) is None
    assert await db.negative_counters() == 0
    usage = await _usage(db, user_id, day)
    assert usage.used_today == 3


async def test_lowering_a_limit_while_a_scan_is_in_flight_refunds_without_going_negative(
    ai_settings_table, settings_clock, downstream
):
    """A scan admitted under the old limit is refunded (provider down) after
    the limit dropped to 0: its units come back exactly, never below 0."""
    db = ai_settings_table
    day, user_id = db.make_day(), await db.make_user()
    await _set("quota.free.daily", "5")

    async def lower_mid_call():
        await _set("quota.free.daily", "0")
        settings_clock.expire()

    with h.FakeProvider("unavailable", on_call=lower_mid_call).patch():
        got = await _scan(db, user_id, day)
    assert got.code == "scan_unavailable"
    month = day.replace(day=1)
    assert await db.row(user_id, feature=h.SCAN, kind="day", start=day) == 0
    assert await db.row(user_id, feature=h.SCAN, kind="month", start=month) is None

    with h.FakeProvider("books").patch() as fake:
        assert (await _scan(db, user_id, day)).code == "daily_scan_limit"
    assert fake.calls == 0
    assert await db.negative_counters() == 0


@pytest.mark.parametrize(
    "key, value",
    [
        ("quota.free.daily", "1001"),
        ("quota.free.daily", "-1"),
        ("quota.free.daily", "2.5"),
        ("quota.free.daily", "true"),
        ("quota.free.daily", "five"),
        ("quota.premium.daily", "1001"),
        ("scan.global_daily_limit", "0"),
        ("scan.global_daily_limit", "1000001"),
        ("breaker.free_daily_ceiling_micros", "-1"),
        ("breaker.free_daily_ceiling_micros", "1000000001"),
        ("scan.enhanced.zero_result_threshold", "21"),
        ("scan.enhanced.zero_result_window_hours", "0"),
        ("scan.enhanced.zero_result_action", "refund"),
    ],
)
async def test_out_of_bounds_values_are_rejected_and_change_nothing(
    ai_settings_table, settings_clock, key, value
):
    db = ai_settings_table
    good = "charge" if key.endswith("action") else "1"
    await _set(key, good, by="ops-before")

    result = await h.cli("set", key, value, "--by", "ops-bad")
    assert result.code == 2
    assert result.err.startswith("error: ")

    row = await h.stored_setting(db.factory, key)
    assert (row.value, row.updated_by) == (
        ai_settings.parse_cli_value(good),
        "ops-before",
    )
    settings_clock.expire()
    async with db.factory() as session:
        current = await ai_settings.get_ai_settings(session)
    assert current.get(key) == ai_settings.parse_cli_value(good)


async def test_a_hand_edited_out_of_bounds_row_is_ignored(
    ai_settings_table, settings_clock, provider, downstream
):
    """A value written around the CLI is never used: the env default is."""
    db = ai_settings_table
    day, user_id = db.make_day(), await db.make_user()
    async with db.factory() as session:
        session.add(AISetting(key="quota.free.daily", value=-3, updated_by="hand"))
        await session.commit()
    async with db.factory() as session:
        current = await ai_settings.get_ai_settings(session)
    default_daily = ai_settings.DEFINITIONS["quota.free.daily"].default()
    assert current.daily_allowance("free") == default_daily
    usage = await _usage(db, user_id, day)
    assert usage.daily_limit == default_daily


@pytest.mark.parametrize("key", sorted(ai_settings.RETIRED_KEYS))
async def test_retired_monthly_keys_are_unknown_and_their_rows_ignored(
    ai_settings_table, settings_clock, provider, downstream, key
):
    """#582: the CLI refuses the retired monthly keys as unknown (nothing is
    stored), and a row the previous release left, even at 0 (which used to
    refuse every scan), limits nothing and is not reported."""
    db = ai_settings_table
    day, user_id = db.make_day(), await db.make_user()
    with pytest.raises(SystemExit):
        await h.cli("set", key, "5", "--by", "ops")
    assert await h.stored_setting(db.factory, key) is None

    async with db.factory() as session:
        session.add(AISetting(key=key, value=0, updated_by="seed"))
        await session.commit()
    settings_clock.expire()
    assert (await _scan(db, user_id, day)).status == 200
    listed = await h.cli("list")
    assert listed.code == 0 and key not in listed.out
    usage = await _usage(db, user_id, day)
    assert (usage.used_month, usage.monthly_limit) == (None, None)


async def test_every_change_records_who_made_it(
    ai_settings_table, monkeypatch, settings_clock
):
    db = ai_settings_table
    await _set("quota.free.daily", "4", by="ops-alice")
    first = await h.stored_setting(db.factory, "quota.free.daily")
    assert (first.value, first.updated_by) == (4, "ops-alice")

    await _set("quota.free.daily", "6", by="ops-bob")
    second = await h.stored_setting(db.factory, "quota.free.daily")
    assert (second.value, second.updated_by) == (6, "ops-bob")
    assert second.updated_at >= first.updated_at

    # Without --by: $USER, else "cli".
    monkeypatch.setenv("USER", "carol")
    assert (await h.cli("set", "quota.free.daily", "7")).code == 0
    third = await h.stored_setting(db.factory, "quota.free.daily")
    assert (third.value, third.updated_by) == (7, "carol")
    monkeypatch.delenv("USER")
    assert (await h.cli("set", "quota.free.daily", "8")).code == 0
    fourth = await h.stored_setting(db.factory, "quota.free.daily")
    assert (fourth.value, fourth.updated_by) == (8, ai_settings.DEFAULT_CLI_ACTOR)

    # A blank actor is refused and changes nothing.
    assert (await h.cli("set", "quota.free.daily", "9", "--by", " ")).code == 2
    assert (await h.stored_setting(db.factory, "quota.free.daily")).value == 8

    # `get` shows who changed it.
    shown = await h.cli("get", "quota.free.daily")
    assert shown.code == 0 and "by cli" in shown.out
    assert timedelta(0) <= fourth.updated_at - first.updated_at

"""Found and no-match counters (#577), on real Postgres: what the matrix
(``test_limits_matrix.py``) cannot express in one row.

- A lost commit acknowledgement on the settle never moves a unit twice (with
  the ledger row as the gate, with no estimate or cost to settle, and
  without a ledger row, where the lease is the gate).
- Parallel no-match scans never pass the no-match ceiling.
- The ceiling warning: one Sentry warning per user per day, with no user
  data, and a log line without the user id.
- ``GET /scan/usage`` reports both counters.
- Settings: the CLI bounds of the no-match keys, the retired
  ``quota.<tier>.daily`` keys (rejected by the CLI, ignored on read), and
  the carry-over of an operator's customised old value.
"""

import asyncio
import logging
import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest

from app.ai import ledger, quota
from app.ai import settings as ai_settings
from app.ai.entitlements import Tier
from app.api import scan as scan_api
from app.models.ai_settings import AISetting
from app.models.user import User
from tests.limits import _harness as h
from tests.limits.test_limits_concurrency import _ack_lost_once
from tests.limits.test_limits_matrix import BASE

pytestmark = pytest.mark.integration


async def _counts(db: h.LimitsDb, user_id, day) -> tuple[int, int]:
    """``(found, no_match)`` for ``day``."""
    c = await db.counters(user_id, day)
    return c.daily, c.no_match


# ---------------------------------------------------------------------------
# Lost commit acknowledgements: the move happens at most once
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier", [Tier.FREE, Tier.PREMIUM], ids=["free", "premium"])
async def test_lost_ack_on_the_settle_moves_the_unit_once(
    limits_db, downstream, monkeypatch, tier
):
    """The settle (ledger row, spend, the move, lease delete) commits, its
    ack is lost and it is retried: the retry finds the ledger row and moves
    nothing. Found ends where it started (taken, then given back once);
    no-match is up by one; the spend is settled once."""
    db = limits_db
    day, user_id = db.make_day(), await db.make_user()
    await db.seed_units(user_id, feature=h.SCAN, kind="day", start=day, units=2)
    await db.seed_units(user_id, feature=h.NO_MATCH, kind="day", start=day, units=1)
    h.use_settings(BASE)
    calls: list = []
    monkeypatch.setattr(
        quota, "release_lease", _ack_lost_once(quota.release_lease, calls)
    )
    with (
        h.Clock(h.at(day)).patch(),
        h.as_tier(tier),
        h.FakeProvider("no_books").patch(),
    ):
        got = await h.call_scan(db.factory, user_id, enhanced=False)
    assert got.status == 200
    assert len(calls) == 2  # retried
    assert await _counts(db, user_id, day) == (2, 2)
    assert (await db.counters(user_id, day)).global_ == 4
    assert await db.spend(day) == (0, h.cost(False, BASE))
    assert await db.lease(user_id) is None
    assert await db.negative_counters() == 0


async def test_lost_ack_without_a_ledger_row_moves_once(
    limits_db, downstream, monkeypatch
):
    """No ledger row could be built: the lease is the gate. The first
    settle moves and deletes the lease, its ack is lost, and the retry
    finds no lease, so it moves nothing."""
    db = limits_db
    day, user_id = db.make_day(), await db.make_user()
    await db.seed_units(user_id, feature=h.SCAN, kind="day", start=day, units=2)
    h.use_settings(BASE)
    calls: list = []
    monkeypatch.setattr(
        quota, "release_lease", _ack_lost_once(quota.release_lease, calls)
    )
    monkeypatch.setattr(scan_api, "_usage_row", lambda **_kw: None)
    with (
        h.Clock(h.at(day)).patch(),
        h.as_tier(Tier.FREE),
        h.FakeProvider("no_books").patch(),
    ):
        got = await h.call_scan(db.factory, user_id, enhanced=False)
    assert got.status == 200
    assert len(calls) == 2
    assert await _counts(db, user_id, day) == (2, 1)
    assert await db.lease(user_id) is None


async def test_retried_settle_with_nothing_to_spend_moves_once(limits_db, monkeypatch):
    """The ledger insert gates the move even when the settle has no
    estimate and no cost to settle (so nothing else asks whether the row was
    inserted)."""
    db = limits_db
    day, user_id = db.make_day(), await db.make_user()
    await db.seed_units(user_id, feature=h.SCAN, kind="day", start=day, units=1)
    calls: list = []
    monkeypatch.setattr(
        quota, "release_lease", _ack_lost_once(quota.release_lease, calls)
    )
    ai = ai_settings.AISettings(dict(BASE))
    with h.Clock(h.at(day)).patch(), h.as_tier(Tier.FREE):
        async with db.factory() as session:
            user = await session.get(User, user_id)
            reservation = await quota.reserve(
                session,
                user=user,
                feature=quota.SCAN_QUOTA_FEATURE,
                timeout_s=10,
                ai=ai,
                est_cost_micros=0,
            )
            row = ledger.usage_insert(
                call_id=uuid.uuid4(),
                user_id=user_id,
                feature="scan.standard",
                model="gpt-4o-mini",
                status="ok",
                input_tokens=0,
                output_tokens=0,
            )
            moved = await quota.release(
                session, reservation, row, None, None, quota.NoMatch()
            )
    assert len(calls) == 2
    # The first attempt moved (its ack lost); the retry inserted nothing and
    # so decided nothing, and reports nothing.
    assert moved is None
    assert await _counts(db, user_id, day) == (1, 1)


async def test_move_on_the_same_held_lease_is_a_no_op_the_second_time(limits_db):
    """The move clears ``daily_taken`` in the same transaction, and only a
    lease that still carries it may move. A second move on the same still-held
    lease therefore decides nothing, and the found unit is not given back
    twice: found goes down once, no-match up once."""
    db = limits_db
    day, user_id = db.make_day(), await db.make_user()
    h.use_settings(BASE)
    ai = ai_settings.AISettings(dict(BASE))
    with h.Clock(h.at(day)).patch(), h.as_tier(Tier.FREE):
        async with db.factory() as session:
            user = await session.get(User, user_id)
            reservation = await quota.reserve(
                session,
                user=user,
                feature=quota.SCAN_QUOTA_FEATURE,
                timeout_s=10,
                ai=ai,
                est_cost_micros=0,
            )
            await session.commit()
        assert await db.lease(user_id) == reservation.lease_id
        assert await _counts(db, user_id, day) == (1, 0)
        results = []
        for _ in range(2):
            async with db.factory() as session:
                results.append(await quota.move_to_no_match(session, reservation))
                await session.commit()
    assert results[0] is not None
    assert results[1] is None
    assert await db.lease(user_id) == reservation.lease_id  # still held
    assert await _counts(db, user_id, day) == (0, 1)
    assert await db.negative_counters() == 0


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


N = 12
L = 3


async def _scan_until_decided(db, user_id) -> object:
    for _ in range(1_000):
        got = await h.call_scan(db.factory, user_id, enhanced=False)
        if got.code != "scan_in_progress":
            return got
        await asyncio.sleep(0.005)
    raise AssertionError("gave up")


@pytest.mark.parametrize("tier", [Tier.FREE, Tier.PREMIUM], ids=["free", "premium"])
async def test_parallel_no_match_scans_never_pass_the_no_match_ceiling(
    limits_db, downstream, tier
):
    """N no-match scans of one user at once, found room for all, no-match
    ceiling L: exactly L are free, the rest counted as found scans."""
    db = limits_db
    day, user_id = db.make_day(), await db.make_user()
    t = tier.value
    h.use_settings(
        {**BASE, f"quota.{t}.found_daily": 1_000, f"quota.{t}.no_match_daily": L}
    )
    provider = h.FakeProvider("no_books", delay=0.01)
    with h.Clock(h.at(day)).patch(), h.as_tier(tier), provider.patch():
        outcomes = await asyncio.gather(
            *(_scan_until_decided(db, user_id) for _ in range(N))
        )
    assert all(o.status == 200 for o in outcomes)
    fields = sorted(o.response.no_match for o in outcomes)
    assert fields == sorted(["free"] * L + ["counted"] * (N - L))
    assert provider.calls == N
    assert await _counts(db, user_id, day) == (N - L, L)
    assert (await db.counters(user_id, day)).global_ == N


async def test_parallel_moves_on_separate_connections_never_pass_the_ceiling(
    limits_db,
):
    """The no-match take itself, without the lease that serialises one
    user: N concurrent conditional upserts on N connections, ceiling L."""
    db = limits_db
    day, user_id = db.make_day(), await db.make_user()

    async def one():
        async with db.factory() as session:
            return await quota.reserve_unit(session, user_id, h.NO_MATCH, "day", day, L)

    results = await asyncio.gather(*(one() for _ in range(N)))
    assert sorted(r for r in results if r is not None) == list(range(1, L + 1))
    assert await db.units(user_id, feature=h.NO_MATCH, kind="day", start=day) == L


# ---------------------------------------------------------------------------
# Tracking: the ceiling warning
# ---------------------------------------------------------------------------


async def test_ceiling_warning_once_per_user_per_day_without_user_data(
    limits_db, downstream, caplog
):
    db = limits_db
    day, alice, bob = db.make_day(), await db.make_user(), await db.make_user()
    next_day = db.claim_day(day + timedelta(days=1))
    h.use_settings({**BASE, "quota.free.no_match_daily": 2})
    with (
        patch.object(quota.alerts, "capture_service_warning") as capture,
        caplog.at_level(logging.INFO, logger="app.ai.quota"),
        h.as_tier(Tier.FREE),
        h.FakeProvider("no_books").patch(),
    ):
        with h.Clock(h.at(day)).patch():
            alice_fields = [
                (await h.call_scan(db.factory, alice, enhanced=False)).response.no_match
                for _ in range(4)
            ]
            assert capture.call_count == 1  # the 2nd move reached the ceiling
            await h.call_scan(db.factory, bob, enhanced=False)
            await h.call_scan(db.factory, bob, enhanced=False)
            assert capture.call_count == 2  # Bob's own warning
        with h.Clock(h.at(next_day)).patch():
            await h.call_scan(db.factory, alice, enhanced=False)
            await h.call_scan(db.factory, alice, enhanced=False)
    assert alice_fields == ["free", "free", "counted", "counted"]
    assert capture.call_count == 3  # a new day, a new warning
    for call in capture.call_args_list:
        assert call.kwargs == {
            "fingerprint": ["ai-no-match-ceiling"],
            "tags": {"tier": "free"},
            "extras": {"ceiling": 2},
        }
        text = repr(call)
        for uid in (alice, bob):
            assert str(uid) not in text and f"{uid}@example.com" not in text
    assert "no-match scans reached the daily ceiling" in caplog.text
    assert "no-match counted as a scan" in caplog.text
    for uid in (alice, bob):
        assert str(uid) not in caplog.text


async def test_ceiling_warning_is_not_sent_when_the_move_fails(limits_db, downstream):
    """A failed move keeps the scan charged, answers normally and sends no
    warning."""
    db = limits_db
    day, user_id = db.make_day(), await db.make_user()
    h.use_settings({**BASE, "quota.free.no_match_daily": 1})

    def boom(*_a, **_kw):
        raise RuntimeError("move failed")

    # Called inside the move's SAVEPOINT once the no-match unit is taken
    # (the log line of the move that reaches the ceiling): the move is
    # rolled back there.
    with (
        patch.object(quota.alerts, "capture_service_warning") as capture,
        patch.object(quota, "_tier_tag", boom),
        h.Clock(h.at(day)).patch(),
        h.as_tier(Tier.FREE),
        h.FakeProvider("no_books").patch(),
    ):
        got = await h.call_scan(db.factory, user_id, enhanced=False)
    assert got.status == 200 and got.response.no_match is None
    assert capture.call_count == 0
    # Charged as a found scan; the usage row, spend and lease delete still
    # committed.
    assert await _counts(db, user_id, day) == (1, 0)
    assert await db.usage(user_id) == [("ok", h.cost(False, BASE))]
    assert await db.spend(day) == (0, h.cost(False, BASE))
    assert await db.lease(user_id) is None


# ---------------------------------------------------------------------------
# GET /scan/usage
# ---------------------------------------------------------------------------


async def _usage(db: h.LimitsDb, user_id, day):
    with h.Clock(h.at(day)).patch():
        async with db.factory() as session:
            user = await session.get(User, user_id)
            return await scan_api.scan_usage.__wrapped__(
                request=None, current_user=user, db=session
            )


@pytest.mark.parametrize("tier", [Tier.FREE, Tier.PREMIUM], ids=["free", "premium"])
async def test_usage_reports_both_counters(limits_db, downstream, tier):
    db = limits_db
    day, user_id = db.make_day(), await db.make_user()
    t = tier.value
    h.use_settings(
        {**BASE, f"quota.{t}.found_daily": 7, f"quota.{t}.no_match_daily": 4}
    )
    with h.Clock(h.at(day)).patch(), h.as_tier(tier):
        with h.FakeProvider("books").patch():
            await h.call_scan(db.factory, user_id, enhanced=False)
        with h.FakeProvider("no_books").patch():
            await h.call_scan(db.factory, user_id, enhanced=False)
            await h.call_scan(db.factory, user_id, enhanced=False)
        usage = await _usage(db, user_id, day)
    assert (usage.found_used_today, usage.found_daily_limit) == (1, 7)
    assert (usage.no_match_used_today, usage.no_match_daily_limit) == (2, 4)
    # Kept for one release for older app builds: the found counter.
    assert (usage.used_today, usage.daily_limit) == (1, 7)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@pytest.fixture
def settings_clock():
    clock = h.SettingsClock()
    with clock.patch():
        yield clock


NEW_KEYS = [
    "quota.free.found_daily",
    "quota.free.no_match_daily",
    "quota.premium.found_daily",
    "quota.premium.no_match_daily",
]


@pytest.mark.parametrize("key", NEW_KEYS)
@pytest.mark.parametrize("value", ["0", "1000"])
async def test_cli_accepts_the_bounds(ai_settings_table, settings_clock, key, value):
    db = ai_settings_table
    result = await h.cli("set", key, value, "--by", "ops")
    assert result.code == 0, result.err
    assert (await h.stored_setting(db.factory, key)).value == int(value)


@pytest.mark.parametrize("key", NEW_KEYS)
@pytest.mark.parametrize("value", ["1001", "-1", "2.5", "true", "five"])
async def test_cli_rejects_out_of_bounds(ai_settings_table, settings_clock, key, value):
    db = ai_settings_table
    result = await h.cli("set", key, value, "--by", "ops")
    assert result.code == 2 and result.err.startswith("error: ")
    assert await h.stored_setting(db.factory, key) is None


def test_every_new_key_defaults_to_5():
    ai = ai_settings.AISettings({})
    assert [ai.get(k) for k in NEW_KEYS] == [5, 5, 5, 5]
    for key in NEW_KEYS:
        assert key in ai_settings.seed_values()


@pytest.mark.parametrize("key", ["quota.free.daily", "quota.premium.daily"])
async def test_retired_daily_keys_are_rejected_by_the_cli(
    ai_settings_table, settings_clock, key
):
    db = ai_settings_table
    assert key in ai_settings.RETIRED_KEYS and key not in ai_settings.KNOWN_KEYS
    with pytest.raises(SystemExit):
        await h.cli("set", key, "3", "--by", "ops")
    assert await h.stored_setting(db.factory, key) is None
    assert key not in ai_settings.seed_values()


@pytest.mark.parametrize("tier", ["free", "premium"])
async def test_retired_daily_key_is_ignored_when_found_daily_is_stored(
    ai_settings_table, settings_clock, tier
):
    db = ai_settings_table
    async with db.factory() as session:
        session.add(AISetting(key=f"quota.{tier}.daily", value=0, updated_by="ops"))
        session.add(AISetting(key=f"quota.{tier}.found_daily", value=9, updated_by="o"))
        await session.commit()
    async with db.factory() as session:
        current = await ai_settings.get_ai_settings(session)
    assert current.daily_allowance(tier) == 9
    assert f"quota.{tier}.daily" not in current.rows


@pytest.mark.parametrize("tier", ["free", "premium"])
async def test_customised_old_value_is_carried_over_on_read(
    ai_settings_table, settings_clock, tier
):
    """An operator's quota.<tier>.daily, with no found_daily row yet (the
    seed has not run): used as found_daily."""
    db = ai_settings_table
    async with db.factory() as session:
        session.add(AISetting(key=f"quota.{tier}.daily", value=8, updated_by="ops"))
        await session.commit()
    async with db.factory() as session:
        current = await ai_settings.get_ai_settings(session)
    assert current.daily_allowance(tier) == 8
    shown = await h.cli("get", f"quota.{tier}.found_daily")
    assert shown.code == 0 and "= 8" in shown.out and "carried over" in shown.out


@pytest.mark.parametrize("tier", ["free", "premium"])
async def test_customised_old_value_is_carried_over_by_the_seed(
    ai_settings_table, settings_clock, tier
):
    db = ai_settings_table
    async with db.factory() as session:
        session.add(AISetting(key=f"quota.{tier}.daily", value=8, updated_by="ops"))
        await session.commit()
    async with db.factory() as session:
        await ai_settings.seed_ai_settings(session)
    row = await h.stored_setting(db.factory, f"quota.{tier}.found_daily")
    assert (row.value, row.updated_by) == (8, ai_settings.SEED_ACTOR)
    # The no-match key gets its own default; the old row is left alone.
    assert (
        await h.stored_setting(db.factory, f"quota.{tier}.no_match_daily")
    ).value == 5
    assert (await h.stored_setting(db.factory, f"quota.{tier}.daily")).value == 8
    # Once found_daily exists, changing the old row changes nothing.
    async with db.factory() as session:
        old = await session.get(AISetting, f"quota.{tier}.daily")
        old.value = 1
        await session.commit()
    settings_clock.expire()
    async with db.factory() as session:
        assert (await ai_settings.get_ai_settings(session)).daily_allowance(tier) == 8


@pytest.mark.parametrize("tier, old", [("free", 5), ("premium", 30)])
async def test_seeded_old_value_is_not_carried_over(
    ai_settings_table, settings_clock, tier, old
):
    """A quota.<tier>.daily row the seed wrote is only the old env default:
    the new default applies (Premium: 30 before, 5 now)."""
    db = ai_settings_table
    async with db.factory() as session:
        session.add(
            AISetting(
                key=f"quota.{tier}.daily", value=old, updated_by=ai_settings.SEED_ACTOR
            )
        )
        await session.commit()
    async with db.factory() as session:
        assert (await ai_settings.get_ai_settings(session)).daily_allowance(tier) == 5
    async with db.factory() as session:
        await ai_settings.seed_ai_settings(session)
    row = await h.stored_setting(db.factory, f"quota.{tier}.found_daily")
    assert row.value == 5


async def test_an_invalid_old_value_is_not_carried_over(
    ai_settings_table, settings_clock
):
    db = ai_settings_table
    async with db.factory() as session:
        session.add(AISetting(key="quota.free.daily", value=-3, updated_by="hand"))
        await session.commit()
    async with db.factory() as session:
        assert (await ai_settings.get_ai_settings(session)).daily_allowance("free") == 5

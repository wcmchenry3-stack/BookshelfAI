"""Database-free unit tests for the #577 no-match move and its warning.

The behaviour is proven end to end on Postgres in
``tests/limits/test_found_and_no_match.py``; these cover the decision paths
of :func:`quota.move_to_no_match` and :func:`quota.send_no_match_ceiling_warning`
in the job that runs without a database.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai import quota
from app.ai.entitlements import Tier


def _reservation(**kw) -> quota.Reservation:
    base = {
        "user_id": uuid.uuid4(),
        "feature": quota.SCAN_QUOTA_FEATURE,
        "lease_id": uuid.uuid4(),
        "day": quota.utc_today(),
        "tier": Tier.FREE,
        "allowance": quota.Allowance(5, 5),
    }
    base.update(kw)
    return quota.Reservation(**base)


def _result(value):
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _db(*values):
    """A session whose ``execute`` returns ``values`` in order (then None)."""
    db = AsyncMock()
    nested = MagicMock()
    nested.__aenter__ = AsyncMock(return_value=None)
    nested.__aexit__ = AsyncMock(return_value=False)
    db.begin_nested = MagicMock(return_value=nested)
    results = [_result(v) for v in values]
    db.execute = AsyncMock(side_effect=results + [_result(None)] * 5)
    return db


class TestMoveToNoMatch:
    async def test_lease_not_held_or_not_flagged_decides_nothing(self):
        db = _db(None)  # the FOR UPDATE select finds no flagged row
        assert await quota.move_to_no_match(db, _reservation()) is None
        assert db.execute.await_count == 1  # nothing taken or given back

    async def test_moves_under_the_ceiling(self):
        lease = uuid.uuid4()
        db = _db(lease, 2)  # held; the no-match upsert returns count 2
        moved = await quota.move_to_no_match(db, _reservation(lease_id=lease))
        assert moved == quota.NoMatchMoved(quota.NO_MATCH_FREE, 2, False)
        # held select, no-match take, found give-back, flag clear
        assert db.execute.await_count == 4

    async def test_the_move_that_reaches_the_ceiling_claims_the_warning(self):
        lease = uuid.uuid4()
        db = _db(lease, 5)
        moved = await quota.move_to_no_match(db, _reservation(lease_id=lease))
        assert moved == quota.NoMatchMoved(quota.NO_MATCH_FREE, 5, True)

    async def test_no_match_full_counts_the_scan(self):
        lease = uuid.uuid4()
        db = _db(lease, None)  # the conditional upsert refuses: full
        moved = await quota.move_to_no_match(db, _reservation(lease_id=lease))
        assert moved is not None and moved.outcome == quota.NO_MATCH_COUNTED
        assert db.execute.await_count == 2  # no found give-back, no flag clear

    async def test_ceiling_zero_never_takes_and_counts_the_scan(self):
        lease = uuid.uuid4()
        db = _db(lease)
        moved = await quota.move_to_no_match(
            db, _reservation(lease_id=lease, allowance=quota.Allowance(5, 0))
        )
        assert moved is not None and moved.outcome == quota.NO_MATCH_COUNTED
        assert db.execute.await_count == 1  # only the held select

    async def test_a_failure_keeps_the_scan_charged(self):
        db = _db()
        db.begin_nested = MagicMock(side_effect=RuntimeError("savepoint failed"))
        assert await quota.move_to_no_match(db, _reservation()) is None

    async def test_premium_uses_its_own_ceiling(self):
        lease = uuid.uuid4()
        db = _db(lease, 3)
        moved = await quota.move_to_no_match(
            db,
            _reservation(
                lease_id=lease, tier=Tier.PREMIUM, allowance=quota.Allowance(5, 3)
            ),
        )
        assert moved == quota.NoMatchMoved(quota.NO_MATCH_FREE, 3, True)


class TestNoMatchCeilingWarning:
    def test_sends_tier_and_ceiling_only(self):
        with patch.object(quota.alerts, "capture_service_warning") as capture:
            quota.send_no_match_ceiling_warning(tier=Tier.FREE, ceiling=5)
        capture.assert_called_once()
        kwargs = capture.call_args.kwargs
        assert kwargs["fingerprint"] == ["ai-no-match-ceiling"]
        assert kwargs["tags"] == {"tier": "free"}
        assert kwargs["extras"] == {"ceiling": 5}

    def test_unknown_tier_is_tagged_unknown(self):
        with patch.object(quota.alerts, "capture_service_warning") as capture:
            quota.send_no_match_ceiling_warning(tier=None, ceiling=1)
        assert capture.call_args.kwargs["tags"] == {"tier": "unknown"}

    def test_never_raises(self):
        with patch.object(
            quota.alerts, "capture_service_warning", side_effect=RuntimeError("x")
        ):
            quota.send_no_match_ceiling_warning(tier=Tier.FREE, ceiling=5)


@pytest.mark.parametrize("value", [None, 3])
async def test_take_unit_paths(value):
    """``take_unit`` (#590/#577): flag and increment in one commit; a refused
    take clears its flag; a lease that is not ours takes nothing."""
    lease = uuid.uuid4()
    db = _db(lease, value)
    got = await quota.take_unit(
        db,
        uuid.uuid4(),
        lease,
        quota.FLAG_DAILY,
        quota.SCAN_QUOTA_FEATURE,
        quota.WINDOW_DAY,
        quota.utc_today(),
        5,
    )
    assert got == value
    # mark, upsert (+ flag clear when refused)
    assert db.execute.await_count == (3 if value is None else 2)
    db.commit.assert_awaited_once()


async def test_take_unit_on_a_lost_lease_takes_nothing():
    db = _db(None)
    with pytest.raises(quota.ScanInProgress):
        await quota.take_unit(
            db,
            uuid.uuid4(),
            uuid.uuid4(),
            quota.FLAG_DAILY,
            quota.SCAN_QUOTA_FEATURE,
            quota.WINDOW_DAY,
            quota.utc_today(),
            5,
        )
    assert db.execute.await_count == 1


async def test_take_unit_with_a_zero_limit_writes_nothing():
    db = _db()
    got = await quota.take_unit(
        db,
        uuid.uuid4(),
        uuid.uuid4(),
        quota.FLAG_DAILY,
        quota.SCAN_QUOTA_FEATURE,
        quota.WINDOW_DAY,
        quota.utc_today(),
        0,
    )
    assert got is None
    db.execute.assert_not_awaited()


class TestGiveBackUnit:
    """``give_back_unit`` (#590), database-free: the decision paths."""

    async def _call(self, db):
        return await quota.give_back_unit(
            db,
            uuid.uuid4(),
            uuid.uuid4(),
            quota.FLAG_DAILY,
            quota.SCAN_QUOTA_FEATURE,
            quota.WINDOW_DAY,
            quota.utc_today(),
        )

    async def test_flag_cleared_gives_the_unit_back(self):
        db = _db(uuid.uuid4())
        assert await self._call(db) == quota.UNIT_GIVEN_BACK
        assert db.execute.await_count == 2  # clear the flag, then decrement
        db.commit.assert_awaited_once()

    async def test_flag_clear_but_lease_held_is_not_taken(self):
        db = _db(None, uuid.uuid4())
        assert await self._call(db) == quota.UNIT_NOT_TAKEN
        assert db.execute.await_count == 2  # nothing decremented

    async def test_lease_held_by_another_request_is_lost(self):
        db = _db(None, None)
        assert await self._call(db) == quota.UNIT_LEASE_LOST


async def test_refund_unit_commits_one_decrement():
    db = _db()
    await quota.refund_unit(
        db, uuid.uuid4(), quota.SCAN_QUOTA_FEATURE, quota.WINDOW_DAY, quota.utc_today()
    )
    db.execute.assert_awaited_once()
    db.commit.assert_awaited_once()


@pytest.mark.parametrize("value, expected", [(None, 0), (4, 4)])
async def test_units_used_reads_zero_without_a_row(value, expected):
    db = _db(value)
    got = await quota.units_used(
        db, uuid.uuid4(), quota.SCAN_QUOTA_FEATURE, quota.WINDOW_DAY, quota.utc_today()
    )
    assert got == expected


async def test_global_units_today_sums_the_day():
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one.return_value = 7
    db.execute = AsyncMock(return_value=result)
    assert await quota.global_units_today(db, quota.utc_today()) == 7


class TestZeroResultHelpers:
    def _count_db(self, count, oldest=None):
        db = AsyncMock()
        counted = MagicMock()
        counted.scalar_one.return_value = count
        row = MagicMock()
        row.scalar_one_or_none.return_value = oldest
        db.execute = AsyncMock(side_effect=[counted, row])
        return db

    async def test_below_the_threshold_is_not_throttled(self):
        db = self._count_db(1)
        assert await quota.zero_result_throttle(db, uuid.uuid4(), 3, 24) is None
        db.commit.assert_awaited_once()

    async def test_at_the_threshold_waits_for_the_oldest_row(self):
        from datetime import UTC, datetime, timedelta

        oldest = datetime.now(UTC) - timedelta(hours=23)
        db = self._count_db(3, oldest)
        retry = await quota.zero_result_throttle(db, uuid.uuid4(), 3, 24)
        assert 1 <= retry <= 3600
        db.commit.assert_awaited_once()

    async def test_threshold_zero_with_no_rows_waits_the_whole_window(self):
        db = self._count_db(0)
        assert await quota.zero_result_throttle(db, uuid.uuid4(), 0, 2) == 7200

    async def test_a_vanished_row_waits_the_whole_window(self):
        db = self._count_db(3, None)
        assert await quota.zero_result_throttle(db, uuid.uuid4(), 3, 1) == 3600

    @pytest.mark.parametrize("found, expected", [(uuid.uuid4(), True), (None, False)])
    async def test_warned_in_window(self, found, expected):
        db = _db(found)
        assert await quota.warned_in_window(db, uuid.uuid4(), 24) is expected

    @pytest.mark.parametrize(
        "count, threshold, past", [(0, 3, False), (3, 3, True), (0, 0, True)]
    )
    def test_zero_result_charged(self, count, threshold, past):
        assert quota.zero_result_charged(count, threshold) is past


class TestCarriedOver:
    """``settings.carried_over`` (#577), database-free."""

    def test_operator_value_is_carried(self):
        from app.ai import settings as ai_settings

        old = ai_settings.legacy_daily_key("free")
        got = ai_settings.carried_over({old: 9}, {old: "alice"})
        assert got == {ai_settings.found_key("free"): 9}

    def test_seeded_value_is_not_carried(self):
        from app.ai import settings as ai_settings

        old = ai_settings.legacy_daily_key("premium")
        assert ai_settings.carried_over({old: 30}, {old: ai_settings.SEED_ACTOR}) == {}

    def test_existing_found_daily_wins(self):
        from app.ai import settings as ai_settings

        old, new = ai_settings.legacy_daily_key("free"), ai_settings.found_key("free")
        assert ai_settings.carried_over({old: 9, new: 4}, {old: "alice"}) == {}

    def test_invalid_old_value_is_not_carried(self):
        from app.ai import settings as ai_settings

        old = ai_settings.legacy_daily_key("free")
        assert ai_settings.carried_over({old: "lots"}, {old: "alice"}) == {}


class TestSettingsViews:
    def test_no_match_allowance_reads_the_tier_key(self):
        from app.ai import settings as ai_settings

        view = ai_settings.AISettings(rows={ai_settings.no_match_key("free"): 7})
        assert view.no_match_allowance("free") == 7

    def test_a_non_scan_feature_is_always_enabled(self):
        from app.ai import settings as ai_settings

        assert ai_settings.AISettings(rows={}).feature_enabled("other.feature")

    def test_describe_reports_a_carried_over_value(self):
        from app.ai import settings as ai_settings

        old, new = ai_settings.legacy_daily_key("free"), ai_settings.found_key("free")
        row = MagicMock(value=9, updated_by="alice")
        text = ai_settings._describe(new, {old: row})
        assert text.startswith(f"{new} = 9") and "carried over" in text

    def test_describe_without_a_row_uses_the_default(self):
        from app.ai import settings as ai_settings

        new = ai_settings.found_key("free")
        assert "default" in ai_settings._describe(new, {})

    async def test_seed_logs_and_inserts_a_carried_over_value(self, caplog):
        from app.ai import settings as ai_settings

        old = ai_settings.legacy_daily_key("free")
        row = MagicMock(value=9, updated_by="alice")
        db = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = ["k"]
        db.execute = AsyncMock(return_value=result)
        with (
            patch.object(ai_settings, "read_rows", AsyncMock(return_value={old: row})),
            caplog.at_level("INFO", logger=ai_settings.logger.name),
        ):
            inserted = await ai_settings.seed_ai_settings(db)
        assert inserted == 1
        assert "carried over from the retired daily key" in caplog.text
        db.commit.assert_awaited_once()


class TestReleaseWithNoMatch:
    """``release`` runs the move under the settle gate and sends the
    ceiling warning only after the commit (#577)."""

    def _db(self, inserted=True):
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = uuid.uuid4() if inserted else None
        db.execute = AsyncMock(return_value=result)
        return db

    async def _release(self, db, moved, reservation=None):
        move = AsyncMock(return_value=moved)
        warn = MagicMock()
        with (
            patch.object(quota, "move_to_no_match", move),
            patch.object(quota, "send_no_match_ceiling_warning", warn),
            patch.object(quota, "release_lease", AsyncMock(return_value=True)),
        ):
            got = await quota.release(
                db,
                reservation or _reservation(),
                MagicMock(),
                None,
                None,
                quota.NoMatch(),
            )
        return got, move, warn

    async def test_moves_and_returns_the_decision(self):
        decision = quota.NoMatchMoved(quota.NO_MATCH_FREE, 2, False)
        got, move, warn = await self._release(self._db(), decision)
        assert got == decision
        move.assert_awaited_once()
        warn.assert_not_called()

    async def test_the_claiming_move_sends_the_warning_once(self):
        decision = quota.NoMatchMoved(quota.NO_MATCH_FREE, 5, True)
        got, _, warn = await self._release(self._db(), decision)
        assert got == decision
        warn.assert_called_once()
        assert warn.call_args.kwargs["ceiling"] == 5

    async def test_a_retried_settle_does_not_move_again(self):
        decision = quota.NoMatchMoved(quota.NO_MATCH_FREE, 2, True)
        got, move, warn = await self._release(self._db(inserted=False), decision)
        assert got is None
        move.assert_not_awaited()
        warn.assert_not_called()

    async def test_nothing_decided_returns_none(self):
        got, move, warn = await self._release(self._db(), None)
        assert got is None
        move.assert_awaited_once()
        warn.assert_not_called()

    async def test_enhanced_reservations_never_move(self):
        got, move, _ = await self._release(
            self._db(),
            quota.NoMatchMoved(quota.NO_MATCH_FREE, 1, False),
            _reservation(enhanced=True, tier=Tier.PREMIUM),
        )
        assert got is None
        move.assert_not_awaited()

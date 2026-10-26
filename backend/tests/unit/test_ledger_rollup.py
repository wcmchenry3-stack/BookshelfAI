"""Usage-ledger rollup and retention logic (app/ai/ledger.py) with a mocked
session: the aggregation, batching and cutoff maths. The SQL itself runs on
Postgres in tests/integration/test_ai_usage_db.py."""

from datetime import UTC, date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from app.ai import ledger


def _row(ts, feature="scan.standard", model="m", status="ok", i=1, o=2, cost=3):
    return (ts, feature, model, status, i, o, cost)


def _db(*executions):
    """Session whose execute() returns the given results in order."""
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=list(executions))
    return db


def _moved(rows):
    res = MagicMock()
    res.all.return_value = rows
    return res


def _count(n):
    res = MagicMock()
    res.rowcount = n
    return res


def test_utc_day_converts_zone():
    ts = datetime(2026, 3, 1, 23, 0, tzinfo=timezone(timedelta(hours=-3)))
    assert ledger._utc_day(ts) == date(2026, 3, 2)


class TestRollupBatch:
    async def test_nothing_to_move_commits_and_returns_zero(self):
        db = _db(_moved([]))
        assert await ledger._rollup_batch(db, datetime(2026, 3, 1, tzinfo=UTC)) == 0
        assert db.execute.await_count == 1
        db.commit.assert_awaited_once()

    async def test_rows_are_summed_per_day_feature_model_status(self):
        day1 = datetime(2026, 1, 1, 10, tzinfo=UTC)
        day1_late = datetime(2026, 1, 1, 22, tzinfo=UTC)
        day2 = datetime(2026, 1, 2, 1, tzinfo=UTC)
        rows = [
            _row(day1, i=10, o=1, cost=100),
            _row(day1_late, i=5, o=2, cost=50),
            _row(day1, status="error", i=0, o=0, cost=0),
            _row(day2, i=7, o=7, cost=70),
        ]
        db = _db(_moved(rows), MagicMock())
        moved = await ledger._rollup_batch(db, datetime(2026, 3, 1, tzinfo=UTC))
        assert moved == 4

        upsert = db.execute.await_args_list[1].args[0]
        values = upsert.compile().params
        # One aggregated row per distinct key: three, not four.
        assert values["calls_m0"] + values["calls_m1"] + values["calls_m2"] == 4
        by_key = {
            (values[f"day_m{n}"], values[f"status_m{n}"]): (
                values[f"calls_m{n}"],
                values[f"input_tokens_m{n}"],
                values[f"output_tokens_m{n}"],
                values[f"cost_micros_m{n}"],
            )
            for n in range(3)
        }
        assert by_key[(date(2026, 1, 1), "ok")] == (2, 15, 3, 150)
        assert by_key[(date(2026, 1, 1), "error")] == (1, 0, 0, 0)
        assert by_key[(date(2026, 1, 2), "ok")] == (1, 7, 7, 70)
        db.commit.assert_awaited_once()

    async def test_rows_near_midnight_land_on_their_utc_day(self):
        local = timezone(timedelta(hours=-8))
        ts = datetime(2026, 1, 1, 20, 0, tzinfo=local)  # 04:00 UTC on the 2nd
        db = _db(_moved([_row(ts)]), MagicMock())
        await ledger._rollup_batch(db, datetime(2026, 3, 1, tzinfo=UTC))
        values = db.execute.await_args_list[1].args[0].compile().params
        assert values["day_m0"] == date(2026, 1, 2)


class TestPurgeDaily:
    async def test_short_batch_stops(self):
        db = _db(_count(3))
        assert await ledger._purge_daily(db, date(2025, 1, 1)) == 3
        db.commit.assert_awaited_once()

    async def test_null_rowcount_counts_as_zero(self):
        db = _db(_count(None))
        assert await ledger._purge_daily(db, date(2025, 1, 1)) == 0

    async def test_full_batches_continue_up_to_the_cap(self):
        full = ledger.PURGE_BATCH_SIZE
        db = _db(*[_count(full) for _ in range(ledger.PURGE_MAX_BATCHES + 5)])
        total = await ledger._purge_daily(db, date(2025, 1, 1))
        assert total == full * ledger.PURGE_MAX_BATCHES
        assert db.execute.await_count == ledger.PURGE_MAX_BATCHES


class TestRollupCutoffs:
    def test_cutoffs_use_the_retention_constants(self):
        now = datetime(2026, 6, 15, 12, tzinfo=UTC)
        raw, daily = ledger.rollup_cutoffs(now)
        assert raw == now - timedelta(days=ledger.RAW_RETENTION_DAYS)
        assert daily == date(2026, 6, 15) - timedelta(days=ledger.DAILY_RETENTION_DAYS)

    def test_daily_cutoff_uses_the_utc_date(self):
        now = datetime(2026, 6, 15, 23, 0, tzinfo=timezone(timedelta(hours=-5)))
        _, daily = ledger.rollup_cutoffs(now)
        assert daily == date(2026, 6, 16) - timedelta(days=ledger.DAILY_RETENTION_DAYS)


class TestRollupAndPurge:
    async def test_runs_one_batch_then_purges(self, monkeypatch):
        monkeypatch.setattr(ledger, "_rollup_batch", AsyncMock(return_value=5))
        monkeypatch.setattr(ledger, "_purge_daily", AsyncMock(return_value=2))
        now = datetime(2026, 6, 15, tzinfo=UTC)
        assert await ledger.rollup_and_purge(AsyncMock(), now) == (5, 2)
        ledger._rollup_batch.assert_awaited_once()
        assert ledger._rollup_batch.await_args.args[1] == now - timedelta(
            days=ledger.RAW_RETENTION_DAYS
        )

    async def test_rollup_is_bounded(self, monkeypatch):
        full = ledger.PURGE_BATCH_SIZE
        monkeypatch.setattr(ledger, "_rollup_batch", AsyncMock(return_value=full))
        monkeypatch.setattr(ledger, "_purge_daily", AsyncMock(return_value=0))
        rolled, _ = await ledger.rollup_and_purge(AsyncMock())
        assert rolled == full * ledger.PURGE_MAX_BATCHES
        assert ledger._rollup_batch.await_count == ledger.PURGE_MAX_BATCHES

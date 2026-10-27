"""Helpers for refresh-token families and reuse detection (#690)."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.dialects import postgresql

from app.auth import refresh_family as rf
from app.auth.refresh_family import Replay, classify_replay
from app.core.config import Settings
from app.models.refresh_token import RefreshToken
from tests.unit._sentry_capture import captured_events

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def _row(*, revoked=False, used=False) -> RefreshToken:
    return RefreshToken(
        id=uuid.uuid4(),
        jti=str(uuid.uuid4()),
        user_id=uuid.uuid4(),
        expires_at=NOW + timedelta(days=7),
        revoked_at=NOW if revoked else None,
        used_at=NOW if used else None,
    )


class TestRows:
    def test_new_family_row_is_its_own_family(self):
        uid = uuid.uuid4()
        row = rf.new_family_row(uid, "jti-1", NOW)
        assert row.id is not None
        assert row.family_id == row.id
        assert (row.user_id, row.jti, row.expires_at) == (uid, "jti-1", NOW)

    def test_successor_inherits_family(self):
        root = rf.new_family_row(uuid.uuid4(), "a", NOW)
        child = rf.successor_row(root, "b", NOW)
        grandchild = rf.successor_row(child, "c", NOW)
        assert child.id not in (None, root.id)
        assert child.user_id == root.user_id
        assert child.family_id == grandchild.family_id == root.id

    def test_row_without_family_is_its_own(self):
        legacy = _row()
        assert legacy.family_id is None
        assert rf.family_of(legacy) == legacy.id
        assert rf.successor_row(legacy, "x", NOW).family_id == legacy.id

    def test_rotated_needs_both_marks(self):
        assert rf.is_rotated(_row(revoked=True, used=True))
        assert not rf.is_rotated(_row(revoked=True))  # logout / legacy rotation
        assert not rf.is_rotated(_row())
        assert rf.is_live(_row())
        assert not rf.is_live(_row(revoked=True))
        assert not rf.is_live(_row(used=True))


class TestClassifyReplay:
    def _classify(self, *, seen=True, age=5, successor=None, grace=30):
        return classify_replay(
            seen_rotated=seen,
            used_at=NOW - timedelta(seconds=age),
            successor=successor,
            now=NOW,
            grace_seconds=grace,
        )

    def test_rotated_while_waiting_is_a_race(self):
        assert self._classify(seen=False, successor=_row()) is Replay.RACE
        # even long after: what matters is the request saw it live
        assert self._classify(seen=False, age=3600) is Replay.RACE

    def test_live_successor_within_window_is_a_retry(self):
        assert self._classify(successor=_row()) is Replay.RETRY
        assert self._classify(age=30, successor=_row()) is Replay.RETRY

    def test_past_window_is_reuse(self):
        assert self._classify(age=31, successor=_row()) is Replay.REUSE
        assert self._classify(age=31, successor=None) is Replay.REUSE

    def test_rotated_successor_is_reuse_within_window(self):
        rotated = _row(revoked=True, used=True)
        assert self._classify(successor=rotated) is Replay.REUSE

    def test_logged_out_or_missing_successor_within_window_is_ended(self):
        assert self._classify(successor=_row(revoked=True)) is Replay.ENDED
        assert self._classify(successor=None) is Replay.ENDED

    def test_zero_grace(self):
        assert self._classify(age=1, successor=_row(), grace=0) is Replay.REUSE
        assert self._classify(age=0, successor=_row(), grace=0) is Replay.RETRY


class TestGraceSetting:
    def test_default_and_bounds(self):
        assert Settings.model_fields["refresh_reuse_grace_seconds"].default == 30
        with pytest.raises(ValueError):
            Settings(refresh_reuse_grace_seconds=-1)
        with pytest.raises(ValueError):
            Settings(refresh_reuse_grace_seconds=121)


class TestAlert:
    def test_event_has_ids_only(self):
        user_id, family_id = uuid.uuid4(), uuid.uuid4()
        with captured_events() as events:
            rf.alert_reuse(user_id, family_id)
        (event,) = events
        assert event["level"] == "warning"
        assert event["message"] == rf.REUSE_ALERT_MESSAGE
        assert event["tags"] == {
            "user_id": str(user_id),
            "family_id": str(family_id),
        }
        assert event["fingerprint"] == list(rf.REUSE_ALERT_FINGERPRINT)
        assert not event.get("extra")

    def test_sentry_failure_is_swallowed(self, caplog):
        with patch.object(
            rf, "capture_service_warning", side_effect=RuntimeError("down")
        ):
            rf.alert_reuse(uuid.uuid4(), uuid.uuid4())
        assert "alert not sent (RuntimeError)" in caplog.text


class TestPurge:
    def test_condition(self):
        sql = str(
            rf.purgeable(NOW).compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )
        assert "refresh_tokens.expires_at <" in sql
        assert "refresh_tokens.revoked_at IS NOT NULL" in sql
        assert "refresh_tokens.used_at IS NULL" in sql
        assert " OR " in sql

    @staticmethod
    def _db(rowcounts):
        db = MagicMock()
        db.execute = AsyncMock(side_effect=[MagicMock(rowcount=n) for n in rowcounts])
        db.commit = AsyncMock()
        return db

    async def test_stops_after_a_short_batch(self):
        db = self._db([rf.PURGE_BATCH_SIZE, 7])
        assert await rf.purge_refresh_tokens(db, NOW) == rf.PURGE_BATCH_SIZE + 7
        assert db.execute.await_count == 2
        assert db.commit.await_count == 2
        stmt = db.execute.await_args_list[0].args[0]
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        assert sql.startswith("DELETE FROM refresh_tokens")
        assert "LIMIT" in sql

    async def test_bounded_number_of_batches(self):
        db = self._db([rf.PURGE_BATCH_SIZE] * (rf.PURGE_MAX_BATCHES + 5))
        total = await rf.purge_refresh_tokens(db, NOW)
        assert db.execute.await_count == rf.PURGE_MAX_BATCHES
        assert total == rf.PURGE_BATCH_SIZE * rf.PURGE_MAX_BATCHES

    async def test_none_rowcount_counts_as_zero(self):
        db = self._db([None])
        assert await rf.purge_refresh_tokens(db, NOW) == 0

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


def _row(*, revoked=False, used=False, displaced=False) -> RefreshToken:
    return RefreshToken(
        id=uuid.uuid4(),
        jti=str(uuid.uuid4()),
        user_id=uuid.uuid4(),
        expires_at=NOW + timedelta(days=7),
        revoked_at=NOW if revoked or displaced else None,
        used_at=NOW if used or displaced else None,
        displaced_at=NOW if displaced else None,
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

    def test_displaced(self):
        displaced = _row(displaced=True)
        assert rf.is_displaced(displaced)
        assert rf.is_rotated(displaced) and not rf.is_live(displaced)
        assert not rf.is_displaced(_row(revoked=True, used=True))
        assert not rf.is_displaced(_row())


class TestClassifyReplay:
    def _classify(self, *, seen=True, displaced=False, age=5, successor=None, grace=30):
        return classify_replay(
            seen_rotated=seen,
            displaced=displaced,
            used_at=NOW - timedelta(seconds=age),
            successor=successor,
            now=NOW,
            grace_seconds=grace,
        )

    def test_rotated_while_waiting_with_live_successor_is_a_race(self):
        assert self._classify(seen=False, successor=_row()) is Replay.RACE
        # even long after: what matters is the request saw it live
        assert self._classify(seen=False, age=3600, successor=_row()) is Replay.RACE

    def test_race_whose_twin_successor_is_gone_is_ended(self):
        """No live, unused successor to displace: the plain 401 (no alert)."""
        for successor in (
            None,
            _row(revoked=True),
            _row(revoked=True, used=True),
            _row(displaced=True),  # a third concurrent request
        ):
            assert self._classify(seen=False, successor=successor) is Replay.ENDED

    def test_live_successor_within_window_is_a_retry(self):
        assert self._classify(successor=_row()) is Replay.RETRY
        assert self._classify(age=30, successor=_row()) is Replay.RETRY

    def test_past_window_is_reuse(self):
        assert self._classify(age=31, successor=_row()) is Replay.REUSE
        assert self._classify(age=31, successor=None) is Replay.REUSE

    def test_rotated_successor_is_reuse_within_window(self):
        rotated = _row(revoked=True, used=True)
        assert self._classify(successor=rotated) is Replay.REUSE

    def test_displaced_successor_is_reuse_within_window(self):
        """The token already had its one retry (or won-race replacement)."""
        assert self._classify(age=0, successor=_row(displaced=True)) is Replay.REUSE

    def test_displaced_token_is_always_reuse(self):
        """No grace window, and even when this request saw it live (its
        holder's refresh was in flight when a retry displaced it)."""
        for seen in (True, False):
            for successor in (None, _row(), _row(revoked=True)):
                assert (
                    self._classify(
                        seen=seen, displaced=True, age=0, successor=successor
                    )
                    is Replay.REUSE
                )

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
    def _db(batches, *, predecessors=None, locked_predecessors=None):
        """A session whose purge statements return, per batch, the candidate
        ids in ``batches`` (SELECT ... FOR UPDATE SKIP LOCKED), the
        predecessors pointing at them (``predecessors``: pred id -> candidate
        id), of which ``locked_predecessors`` are the ones this transaction
        could lock, and a DELETE rowcount equal to the ids it was given."""
        predecessors = predecessors or {}
        locked = (
            set(predecessors) if locked_predecessors is None else locked_predecessors
        )
        batches = list(batches)
        db = MagicMock()
        db.statements = []
        db.deleted = []

        async def execute(stmt):
            sql = str(stmt.compile(dialect=postgresql.dialect()))
            db.statements.append(sql)
            result = MagicMock()
            if sql.startswith("DELETE"):
                ids = list(stmt.whereclause.right.value)
                db.deleted.append(ids)
                result.rowcount = len(ids)
            elif "replaced_by_id IN" in sql and "FOR UPDATE" in sql:
                result.scalars.return_value.all.return_value = sorted(locked)
            elif "replaced_by_id IN" in sql:
                result.all.return_value = list(predecessors.items())
            else:
                result.scalars.return_value.all.return_value = batches.pop(0)
            return result

        db.execute = AsyncMock(side_effect=execute)
        db.commit = AsyncMock()
        return db

    @staticmethod
    def _ids(n):
        return [uuid.uuid4() for _ in range(n)]

    async def test_stops_after_a_short_batch(self):
        full, short = self._ids(3), self._ids(2)
        with patch.object(rf, "PURGE_BATCH_SIZE", 3):
            db = self._db([full, short])
            assert await rf.purge_refresh_tokens(db, NOW) == 5
        assert db.deleted == [full, short]
        assert db.commit.await_count == 2
        first = db.statements[0]
        assert first.startswith("SELECT refresh_tokens.id")
        assert "LIMIT" in first and first.endswith("FOR UPDATE SKIP LOCKED")

    async def test_bounded_number_of_batches(self):
        with patch.object(rf, "PURGE_BATCH_SIZE", 2):
            db = self._db([self._ids(2) for _ in range(rf.PURGE_MAX_BATCHES + 5)])
            total = await rf.purge_refresh_tokens(db, NOW)
        assert len(db.deleted) == rf.PURGE_MAX_BATCHES
        assert total == 2 * rf.PURGE_MAX_BATCHES

    async def test_nothing_to_purge(self):
        db = self._db([[]])
        assert await rf.purge_refresh_tokens(db, NOW) == 0
        assert db.deleted == []
        assert db.commit.await_count == 1

    async def test_none_rowcount_counts_as_zero(self):
        db = self._db([self._ids(1)])
        db.execute.side_effect, inner = None, db.execute.side_effect

        async def execute(stmt):
            result = await inner(stmt)
            result.rowcount = None
            return result

        db.execute.side_effect = execute
        assert await rf.purge_refresh_tokens(db, NOW) == 0

    async def test_locks_predecessors_and_skips_rows_whose_predecessor_is_locked(
        self,
    ):
        """#690 review C1: deleting a row nulls its predecessor's
        replaced_by_id, which needs the predecessor's lock; a refresh replaying
        that predecessor holds it and waits for the row being deleted. The
        purge locks predecessors SKIP LOCKED and leaves out rows whose
        predecessor someone else holds, instead of waiting (deadlock)."""
        free, blocked, no_pred = self._ids(3)
        free_pred, busy_pred = self._ids(2)
        db = self._db(
            [[free, blocked, no_pred]],
            predecessors={free_pred: free, busy_pred: blocked},
            locked_predecessors={free_pred},
        )
        assert await rf.purge_refresh_tokens(db, NOW) == 2
        assert db.deleted == [[free, no_pred]]
        lock_preds = db.statements[1]
        assert "refresh_tokens.replaced_by_id IN" in lock_preds
        assert lock_preds.endswith("FOR UPDATE SKIP LOCKED")

    async def test_all_candidates_blocked_deletes_nothing(self):
        (row,) = self._ids(1)
        (pred,) = self._ids(1)
        db = self._db([[row]], predecessors={pred: row}, locked_predecessors=set())
        assert await rf.purge_refresh_tokens(db, NOW) == 0
        assert db.deleted == []
        assert db.commit.await_count == 1

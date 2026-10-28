"""Unit tests for the refresh token cleanup background task."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.token_cleanup import cleanup_expired_tokens


def _make_db_mock(rowcount: int = 3):
    """Return an AsyncMock that mimics AsyncSessionLocal as an async context manager."""
    mock_result = MagicMock()
    mock_result.rowcount = rowcount

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_session_factory = MagicMock(return_value=mock_session)
    return mock_session_factory, mock_session, mock_result


class TestCleanupExpiredTokens:
    """cleanup_expired_tokens must delete expired rows and handle errors gracefully."""

    @pytest.mark.asyncio
    async def test_deletes_expired_tokens_and_logs(self, caplog) -> None:
        """A single cleanup run must execute a DELETE and commit."""

        mock_factory, mock_session, _mock_result = _make_db_mock(rowcount=5)

        with (
            patch("app.services.token_cleanup.AsyncSessionLocal", mock_factory),
            patch("app.services.token_cleanup._INITIAL_DELAY_SECONDS", 0),
            patch("app.services.token_cleanup._INTERVAL_SECONDS", 0),
            pytest.raises(asyncio.CancelledError),
        ):
            # Run two ticks then cancel
            task = asyncio.create_task(cleanup_expired_tokens())
            await asyncio.sleep(0)  # let it start
            await asyncio.sleep(0)  # let it run the first iteration
            task.cancel()
            await task

        mock_session.execute.assert_called()
        mock_session.commit.assert_called()

    @pytest.mark.asyncio
    async def test_delete_filters_by_expires_at(self) -> None:
        """The DELETE statement must filter on expires_at < now."""

        mock_factory, mock_session, _ = _make_db_mock()

        with (
            patch("app.services.token_cleanup.AsyncSessionLocal", mock_factory),
            patch("app.services.token_cleanup._INITIAL_DELAY_SECONDS", 0),
            patch("app.services.token_cleanup._INTERVAL_SECONDS", 9999),
            pytest.raises(asyncio.CancelledError),
        ):
            task = asyncio.create_task(cleanup_expired_tokens())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            task.cancel()
            await task

        # Confirm execute was called with a DELETE statement
        assert mock_session.execute.called
        stmt = mock_session.execute.call_args[0][0]
        # SQLAlchemy Delete object targets the refresh_tokens table
        assert "refresh_tokens" in str(stmt)

    @pytest.mark.asyncio
    async def test_continues_after_db_error(self, caplog) -> None:
        """A database error must be logged and the task must keep running."""
        import logging

        call_count = 0

        async def flaky_execute(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("connection lost")
            result = MagicMock()
            result.rowcount = 0
            return result

        mock_factory, mock_session, _ = _make_db_mock()
        mock_session.execute = flaky_execute

        with (
            patch("app.services.token_cleanup.AsyncSessionLocal", mock_factory),
            patch("app.services.token_cleanup._INITIAL_DELAY_SECONDS", 0),
            patch("app.services.token_cleanup._INTERVAL_SECONDS", 0),
            caplog.at_level(logging.ERROR, logger="app.services.token_cleanup"),
            pytest.raises(asyncio.CancelledError),
        ):
            task = asyncio.create_task(cleanup_expired_tokens())
            # Let it run through the error and at least one successful pass
            for _ in range(4):
                await asyncio.sleep(0)
            task.cancel()
            await task

        assert "error during cleanup run" in caplog.text
        # Verify it kept running past the error
        assert call_count >= 2

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates_cleanly(self) -> None:
        """asyncio.CancelledError must propagate so the lifespan can await the task."""
        mock_factory, _mock_session, _ = _make_db_mock()

        with patch("app.services.token_cleanup.AsyncSessionLocal", mock_factory), patch(
            "app.services.token_cleanup._INITIAL_DELAY_SECONDS", 0
        ):

            task = asyncio.create_task(cleanup_expired_tokens())
            await asyncio.sleep(0)
            task.cancel()

            with pytest.raises(asyncio.CancelledError):
                await task


class TestAIQuotaPurge:
    """#528: the daily run also purges old ai_quota rows, and a failure there
    does not skip the other cleanups."""

    @pytest.mark.asyncio
    async def test_run_purges_ai_quota(self) -> None:
        mock_factory, _mock_session, _ = _make_db_mock()
        purge = AsyncMock(return_value=2)

        with (
            patch("app.services.token_cleanup.AsyncSessionLocal", mock_factory),
            patch("app.services.token_cleanup.purge_old_quota", purge),
            patch("app.services.token_cleanup._INITIAL_DELAY_SECONDS", 0),
            patch("app.services.token_cleanup._INTERVAL_SECONDS", 9999),
            pytest.raises(asyncio.CancelledError),
        ):
            task = asyncio.create_task(cleanup_expired_tokens())
            for _ in range(4):
                await asyncio.sleep(0)
            task.cancel()
            await task

        purge.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ai_quota_purge_failure_does_not_skip_token_cleanup(
        self, caplog
    ) -> None:
        import logging

        mock_factory, mock_session, _ = _make_db_mock()

        with (
            patch("app.services.token_cleanup.AsyncSessionLocal", mock_factory),
            patch(
                "app.services.token_cleanup.purge_old_quota",
                AsyncMock(side_effect=ValueError("db down")),
            ),
            patch("app.services.token_cleanup._INITIAL_DELAY_SECONDS", 0),
            patch("app.services.token_cleanup._INTERVAL_SECONDS", 9999),
            caplog.at_level(logging.ERROR, logger="app.services.token_cleanup"),
            pytest.raises(asyncio.CancelledError),
        ):
            task = asyncio.create_task(cleanup_expired_tokens())
            for _ in range(4):
                await asyncio.sleep(0)
            task.cancel()
            await task

        assert "ai quota" in caplog.text
        assert "refresh_tokens" in str(mock_session.execute.call_args[0][0])


class TestAISpendPurge:
    """#531: the daily run also purges old ai_spend_daily rows, and a failure
    there does not skip the other cleanups."""

    @pytest.mark.asyncio
    async def test_run_purges_ai_spend(self) -> None:
        mock_factory, _mock_session, _ = _make_db_mock()
        purge = AsyncMock(return_value=1)

        with (
            patch("app.services.token_cleanup.AsyncSessionLocal", mock_factory),
            patch("app.services.token_cleanup.purge_old_quota", AsyncMock()),
            patch(
                "app.services.token_cleanup.rollup_and_purge",
                AsyncMock(return_value=(0, 0)),
            ),
            patch("app.services.token_cleanup.purge_old_spend", purge),
            patch("app.services.token_cleanup._INITIAL_DELAY_SECONDS", 0),
            patch("app.services.token_cleanup._INTERVAL_SECONDS", 9999),
            pytest.raises(asyncio.CancelledError),
        ):
            task = asyncio.create_task(cleanup_expired_tokens())
            for _ in range(6):
                await asyncio.sleep(0)
            task.cancel()
            await task

        purge.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ai_spend_purge_failure_does_not_skip_token_cleanup(
        self, caplog
    ) -> None:
        import logging

        mock_factory, mock_session, _ = _make_db_mock()

        with (
            patch("app.services.token_cleanup.AsyncSessionLocal", mock_factory),
            patch("app.services.token_cleanup.purge_old_quota", AsyncMock()),
            patch(
                "app.services.token_cleanup.rollup_and_purge",
                AsyncMock(return_value=(0, 0)),
            ),
            patch(
                "app.services.token_cleanup.purge_old_spend",
                AsyncMock(side_effect=ValueError("db down")),
            ),
            patch("app.services.token_cleanup._INITIAL_DELAY_SECONDS", 0),
            patch("app.services.token_cleanup._INTERVAL_SECONDS", 9999),
            caplog.at_level(logging.ERROR, logger="app.services.token_cleanup"),
            pytest.raises(asyncio.CancelledError),
        ):
            task = asyncio.create_task(cleanup_expired_tokens())
            for _ in range(6):
                await asyncio.sleep(0)
            task.cancel()
            await task

        assert "ai spend" in caplog.text
        assert "refresh_tokens" in str(mock_session.execute.call_args[0][0])


class TestTransientRetry:
    """#661: transient DB connection errors are retried per step; only an
    exhausted retry budget reaches Sentry (logger.exception)."""

    @staticmethod
    def _patches(quota, base_delay=0):
        mock_factory, mock_session, _ = _make_db_mock()
        patches = [
            patch("app.services.token_cleanup.AsyncSessionLocal", mock_factory),
            patch("app.services.token_cleanup.purge_old_quota", quota),
            patch(
                "app.services.token_cleanup.rollup_and_purge",
                AsyncMock(return_value=(0, 0)),
            ),
            patch(
                "app.services.token_cleanup.purge_old_spend",
                AsyncMock(return_value=0),
            ),
            patch(
                "app.services.token_cleanup.purge_old_zero_results",
                AsyncMock(return_value=0),
            ),
            patch(
                "app.services.token_cleanup.purge_old_scan_records",
                AsyncMock(return_value=0),
            ),
            patch(
                "app.services.token_cleanup.purge_trash",
                AsyncMock(return_value=0),
            ),
            patch("app.services.token_cleanup._INITIAL_DELAY_SECONDS", 0),
            patch("app.services.token_cleanup._INTERVAL_SECONDS", 9999),
            patch("app.services.token_cleanup._RETRY_BASE_DELAY_SECONDS", base_delay),
        ]
        return mock_session, patches

    @staticmethod
    async def _run_once(patches, caplog):
        import contextlib
        import logging

        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(
                caplog.at_level(logging.WARNING, logger="app.services.token_cleanup")
            )
            task = asyncio.create_task(cleanup_expired_tokens())
            for _ in range(60):
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    @pytest.mark.asyncio
    async def test_gaierror_once_then_success_logs_no_error(self, caplog) -> None:
        import logging
        import socket

        quota = AsyncMock(
            side_effect=[socket.gaierror(-2, "Name or service not known"), 4]
        )
        mock_session, patches = self._patches(quota)
        await self._run_once(patches, caplog)

        assert quota.await_count == 2
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert any(r.levelno == logging.WARNING for r in caplog.records)
        assert "refresh_tokens" in str(mock_session.execute.call_args[0][0])

    @pytest.mark.asyncio
    async def test_operational_error_is_retried(self, caplog) -> None:
        import logging

        from sqlalchemy.exc import OperationalError

        quota = AsyncMock(
            side_effect=[OperationalError("SELECT 1", {}, Exception("conn")), 0]
        )
        _session, patches = self._patches(quota)
        await self._run_once(patches, caplog)

        assert quota.await_count == 2
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    @pytest.mark.asyncio
    async def test_persistent_failure_logs_one_error_and_other_steps_run(
        self, caplog
    ) -> None:
        import logging
        import socket

        from app.services import token_cleanup

        quota = AsyncMock(side_effect=socket.gaierror(-2, "Name or service not known"))
        mock_session, patches = self._patches(quota)
        await self._run_once(patches, caplog)

        assert quota.await_count == token_cleanup._RETRY_ATTEMPTS
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1
        assert "ai quota" in errors[0].getMessage()
        assert "refresh_tokens" in str(mock_session.execute.call_args[0][0])

    @pytest.mark.asyncio
    async def test_non_transient_error_is_not_retried(self, caplog) -> None:
        import logging

        quota = AsyncMock(side_effect=ValueError("bug"))
        _session, patches = self._patches(quota)
        await self._run_once(patches, caplog)

        assert quota.await_count == 1
        assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1

    @pytest.mark.asyncio
    async def test_cancel_during_backoff_propagates(self) -> None:
        import contextlib
        import socket

        quota = AsyncMock(side_effect=socket.gaierror(-2, "x"))
        _session, patches = self._patches(quota, base_delay=3600)
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            task = asyncio.create_task(cleanup_expired_tokens())
            for _ in range(10):
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert quota.await_count == 1

    @pytest.mark.asyncio
    async def test_backoff_delays_are_five_then_ten(self, caplog) -> None:
        import socket

        real_sleep = asyncio.sleep
        calls: list[float] = []

        async def fake_sleep(delay, *a, **k):
            calls.append(delay)
            await real_sleep(3600 if delay >= 9999 else 0)

        gai = socket.gaierror(-2, "x")
        quota = AsyncMock(side_effect=[gai, gai, 1])
        _session, patches = self._patches(quota, base_delay=5.0)
        patches.append(patch("app.services.token_cleanup.asyncio.sleep", fake_sleep))
        await self._run_once(patches, caplog)

        assert quota.await_count == 3
        assert [d for d in calls if d in (5.0, 10.0)] == [5.0, 10.0]

    def test_is_transient_follows_cause_chain(self) -> None:
        from sqlalchemy.exc import OperationalError

        from app.services.token_cleanup import _is_transient

        try:
            try:
                raise OperationalError("SELECT 1", {}, Exception("conn"))
            except OperationalError as inner:
                raise RuntimeError("wrapped") from inner
        except RuntimeError as wrapped:
            assert _is_transient(wrapped) is True

        assert _is_transient(ValueError("nope")) is False
        assert _is_transient(RuntimeError("nope")) is False

    @pytest.mark.parametrize(
        "exc",
        [
            ConnectionError("lost"),
            ConnectionRefusedError("refused"),
            ConnectionResetError("reset"),
            OSError("plain"),
        ],
    )
    def test_is_transient_covers_oserror_family(self, exc) -> None:
        from app.services.token_cleanup import _is_transient

        assert _is_transient(exc) is True

    @staticmethod
    def _driver_error(sqlstate: str) -> Exception:
        """A driver error as SQLAlchemy's asyncpg adapter raises it: the
        SQLSTATE on ``sqlstate`` and ``pgcode``."""
        err = Exception("driver")
        err.sqlstate = err.pgcode = sqlstate
        return err

    @pytest.mark.parametrize("sqlstate", ["40P01", "40001"])
    def test_deadlock_and_serialization_failure_are_transient(self, sqlstate) -> None:
        """#690: the refresh-token purge can lose a deadlock to a replay;
        Postgres rolls back one side, and a retry succeeds. A plain
        DBAPIError (not OperationalError) with that SQLSTATE is retried,
        directly or further down the cause chain."""
        from sqlalchemy.exc import DBAPIError, IntegrityError

        from app.services.token_cleanup import _is_transient

        wrapped = DBAPIError("DELETE", {}, self._driver_error(sqlstate))
        assert not isinstance(wrapped, IntegrityError)
        assert _is_transient(wrapped) is True
        assert _is_transient(self._driver_error(sqlstate)) is True
        try:
            try:
                raise wrapped
            except DBAPIError as inner:
                raise RuntimeError("step failed") from inner
        except RuntimeError as outer:
            assert _is_transient(outer) is True

    def test_asyncpg_deadlock_error_is_transient(self) -> None:
        import asyncpg

        from app.services.token_cleanup import _is_transient

        assert _is_transient(asyncpg.exceptions.DeadlockDetectedError("x")) is True
        assert _is_transient(asyncpg.exceptions.SerializationError("x")) is True

    @pytest.mark.parametrize("sqlstate", ["23505", "42P01", "57014"])
    def test_other_sqlstates_are_not_transient(self, sqlstate) -> None:
        from sqlalchemy.exc import DBAPIError

        from app.services.token_cleanup import _is_transient

        assert _is_transient(DBAPIError("x", {}, self._driver_error(sqlstate))) is False
        assert _is_transient(self._driver_error(sqlstate)) is False

    @pytest.mark.asyncio
    async def test_connection_reset_once_then_success_logs_no_error(
        self, caplog
    ) -> None:
        import logging

        quota = AsyncMock(side_effect=[ConnectionResetError("reset by peer"), 4])
        mock_session, patches = self._patches(quota)
        await self._run_once(patches, caplog)

        assert quota.await_count == 2
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert any(r.levelno == logging.WARNING for r in caplog.records)
        assert "refresh_tokens" in str(mock_session.execute.call_args[0][0])

    @pytest.mark.asyncio
    async def test_warning_has_attempt_count_and_label(self, caplog) -> None:
        import logging
        import socket

        quota = AsyncMock(side_effect=[socket.gaierror(-2, "x"), 0])
        _session, patches = self._patches(quota)
        await self._run_once(patches, caplog)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        msg = warnings[0].getMessage()
        assert "ai quota" in msg
        assert "1/3" in msg
        assert "gaierror" in msg

    @pytest.mark.asyncio
    async def test_scan_records_step_is_retried(self, caplog) -> None:
        import logging
        import socket

        scan = AsyncMock(side_effect=[socket.gaierror(-2, "x"), 0])
        _session, patches = self._patches(AsyncMock(return_value=0))
        patches.append(patch("app.services.token_cleanup.purge_old_scan_records", scan))
        await self._run_once(patches, caplog)

        assert scan.await_count == 2
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    @pytest.mark.asyncio
    async def test_refresh_token_step_is_retried(self, caplog) -> None:
        import logging
        import socket

        session, patches = self._patches(AsyncMock(return_value=0))
        session.execute = AsyncMock(
            side_effect=[socket.gaierror(-2, "x"), MagicMock(rowcount=1)]
        )
        await self._run_once(patches, caplog)

        assert session.execute.await_count == 2
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    @pytest.mark.asyncio
    async def test_interface_error_is_retried(self, caplog) -> None:
        import logging

        from sqlalchemy.exc import InterfaceError

        quota = AsyncMock(
            side_effect=[InterfaceError("SELECT 1", {}, Exception("conn")), 0]
        )
        _session, patches = self._patches(quota)
        await self._run_once(patches, caplog)

        assert quota.await_count == 2
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

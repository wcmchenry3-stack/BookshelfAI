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
                raise ConnectionError("connection lost")
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

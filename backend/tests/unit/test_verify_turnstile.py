"""Unit tests for _verify_turnstile — exercises the function body directly."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.api.scan import _verify_turnstile


class TestVerifyTurnstile:
    async def test_returns_true_on_success(self):
        resp = MagicMock()
        resp.json.return_value = {"success": True}
        resp.raise_for_status = MagicMock()

        client = AsyncMock()
        client.post = AsyncMock(return_value=resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("app.api._bot_check.httpx.AsyncClient", return_value=client):
            result = await _verify_turnstile("valid-token")

        assert result is True

    async def test_returns_false_on_failure(self):
        resp = MagicMock()
        resp.json.return_value = {"success": False}
        resp.raise_for_status = MagicMock()

        client = AsyncMock()
        client.post = AsyncMock(return_value=resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("app.api._bot_check.httpx.AsyncClient", return_value=client):
            result = await _verify_turnstile("bad-token")

        assert result is False

    async def test_returns_false_on_timeout(self):
        client = AsyncMock()
        client.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("app.api._bot_check.httpx.AsyncClient", return_value=client):
            result = await _verify_turnstile("any-token")

        assert result is False

    async def test_returns_false_on_http_error(self):
        client = AsyncMock()
        client.post = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "server error",
                request=MagicMock(),
                response=MagicMock(status_code=500),
            )
        )
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("app.api._bot_check.httpx.AsyncClient", return_value=client):
            result = await _verify_turnstile("any-token")

        assert result is False

    async def test_posts_correct_payload(self):
        resp = MagicMock()
        resp.json.return_value = {"success": True}
        resp.raise_for_status = MagicMock()

        client = AsyncMock()
        client.post = AsyncMock(return_value=resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("app.api._bot_check.httpx.AsyncClient", return_value=client),
            patch("app.api._bot_check.settings") as mock_settings,
        ):
            mock_settings.turnstile_secret_key = "test-secret"
            await _verify_turnstile("user-token")

        client.post.assert_awaited_once()
        _, kwargs = client.post.call_args
        assert kwargs["data"]["secret"] == "test-secret"
        assert kwargs["data"]["response"] == "user-token"

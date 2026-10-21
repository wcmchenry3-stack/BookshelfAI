"""Unit tests for the shared rate-limit key function."""

from unittest.mock import MagicMock, patch

from app.core.limiter import _get_user_id_or_ip


def _make_request(auth_header: str = "", client_ip: str = "1.2.3.4") -> MagicMock:
    req = MagicMock()
    req.headers = {"Authorization": auth_header} if auth_header else {}
    req.client.host = client_ip
    return req


class TestGetUserIdOrIp:
    def test_returns_ip_when_no_auth_header(self):
        req = _make_request(client_ip="10.0.0.1")
        result = _get_user_id_or_ip(req)
        assert result == "10.0.0.1"

    def test_returns_ip_when_auth_header_not_bearer(self):
        req = _make_request(auth_header="Basic dXNlcjpwYXNz", client_ip="10.0.0.2")
        result = _get_user_id_or_ip(req)
        assert result == "10.0.0.2"

    def test_returns_user_key_for_valid_bearer_token(self):
        req = _make_request(auth_header="Bearer valid.jwt.token", client_ip="10.0.0.3")

        with patch("app.auth.jwt.decode_token") as mock_decode:
            mock_decode.return_value = {"sub": "user-abc-123", "type": "access"}
            result = _get_user_id_or_ip(req)

        assert result == "user:user-abc-123"

    def test_falls_back_to_ip_when_bearer_token_is_invalid(self):
        req = _make_request(auth_header="Bearer bad.token", client_ip="10.0.0.4")

        with patch("app.auth.jwt.decode_token", side_effect=Exception("invalid")):
            result = _get_user_id_or_ip(req)

        assert result == "10.0.0.4"

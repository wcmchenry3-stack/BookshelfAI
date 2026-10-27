"""JWT_ACCESS_TTL_MINUTES and the retired JWT_EXPIRY_HOURS (#689)."""

from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.core.config import Settings

DB = "postgresql+asyncpg://u:p@localhost/db"


class TestAccessTtlSetting:
    def test_default_is_15_minutes(self, monkeypatch):
        monkeypatch.delenv("JWT_ACCESS_TTL_MINUTES", raising=False)
        assert Settings(database_url=DB, _env_file=None).jwt_access_ttl_minutes == 15

    @pytest.mark.parametrize("value", [1, 15, 60])
    def test_accepts_values_in_bounds(self, value):
        assert (
            Settings(
                database_url=DB, jwt_access_ttl_minutes=value
            ).jwt_access_ttl_minutes
            == value
        )

    @pytest.mark.parametrize("value", [0, -5, 61, 1440])
    def test_rejects_values_out_of_bounds(self, value):
        with pytest.raises(ValidationError):
            Settings(database_url=DB, jwt_access_ttl_minutes=value)

    def test_read_from_env(self, monkeypatch):
        monkeypatch.setenv("JWT_ACCESS_TTL_MINUTES", "5")
        assert Settings(database_url=DB).jwt_access_ttl_minutes == 5

    def test_ttl_seconds_follows_setting(self):
        from app.auth import jwt as auth_jwt

        with patch.object(auth_jwt.settings, "jwt_access_ttl_minutes", 7):
            assert auth_jwt.access_token_ttl_seconds() == 420


class TestRetiredExpiryHours:
    def test_unset_has_no_warning(self, monkeypatch):
        monkeypatch.delenv("JWT_EXPIRY_HOURS", raising=False)
        s = Settings(database_url=DB, _env_file=None)
        assert s.jwt_expiry_hours is None
        assert s.deprecated_settings_warnings == []

    def test_env_value_is_ignored_with_warning(self, monkeypatch):
        monkeypatch.setenv("JWT_EXPIRY_HOURS", "24")
        s = Settings(database_url=DB, _env_file=None)
        # It no longer changes the access-token lifetime.
        assert s.jwt_access_ttl_minutes == 15
        (warning,) = s.deprecated_settings_warnings
        assert "JWT_EXPIRY_HOURS" in warning and "ignored" in warning

    def test_leftover_line_in_env_file_does_not_fail_boot(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("JWT_EXPIRY_HOURS=24\n")
        s = Settings(database_url=DB, _env_file=str(env))
        assert s.jwt_access_ttl_minutes == 15
        assert s.deprecated_settings_warnings

    async def test_startup_logs_the_warning(self):
        from app import main

        with (
            patch.object(main.settings, "jwt_expiry_hours", 24),
            patch.object(main, "check_turnstile_configured"),
            patch.object(main.catalog_token, "check_configured"),
            patch.object(main, "logger") as logger,
        ):
            await main._validate_config()
        (call,) = logger.warning.call_args_list
        assert "JWT_EXPIRY_HOURS" in call.args[0]

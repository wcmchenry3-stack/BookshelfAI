"""Unit tests for settings config."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_allowed_emails_list_parsing():
    s = Settings(
        database_url="postgresql+asyncpg://u:p@localhost/db",
        allowed_emails="alice@example.com, bob@example.com",
    )
    assert s.allowed_emails_list == ["alice@example.com", "bob@example.com"]


def test_allowed_emails_list_single():
    s = Settings(
        database_url="postgresql+asyncpg://u:p@localhost/db",
        allowed_emails="solo@example.com",
    )
    assert s.allowed_emails_list == ["solo@example.com"]


def test_allowed_emails_list_empty():
    s = Settings(
        database_url="postgresql+asyncpg://u:p@localhost/db",
        allowed_emails="",
    )
    assert s.allowed_emails_list == []


class TestGoogleClientIds:
    def test_returns_all_configured_ids(self):
        s = Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db",
            google_client_id="web-id",
            google_ios_client_id="ios-id",
            google_android_client_id="android-id",
        )
        assert s.google_client_ids == ["web-id", "ios-id", "android-id"]

    def test_excludes_empty_ids(self):
        s = Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db",
            google_client_id="web-id",
            google_ios_client_id="",
            google_android_client_id="",
        )
        assert s.google_client_ids == ["web-id"]

    def test_returns_empty_when_none_set(self):
        s = Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db",
        )
        assert s.google_client_ids == []


class TestAsyncDatabaseUrl:
    def test_normalizes_postgres_scheme(self):
        s = Settings(database_url="postgres://u:p@localhost/db")
        assert s.async_database_url == "postgresql+asyncpg://u:p@localhost/db"

    def test_normalizes_postgresql_scheme(self):
        s = Settings(database_url="postgresql://u:p@localhost/db")
        assert s.async_database_url == "postgresql+asyncpg://u:p@localhost/db"

    def test_leaves_postgresql_asyncpg_scheme_unchanged(self):
        s = Settings(database_url="postgresql+asyncpg://u:p@localhost/db")
        assert s.async_database_url == "postgresql+asyncpg://u:p@localhost/db"

    def test_leaves_sqlite_url_untouched(self):
        s = Settings(database_url="sqlite+aiosqlite:///./test.db")
        assert s.async_database_url == "sqlite+aiosqlite:///./test.db"


class TestTransactionPoolerPortValidator:
    def test_rejects_transaction_pooler_port_6543(self):
        with pytest.raises(ValidationError, match="6543"):
            Settings(database_url="postgresql://u:p@db.supabase.co:6543/postgres")

    def test_rejects_transaction_pooler_port_with_postgres_scheme(self):
        with pytest.raises(ValidationError, match="session pooler"):
            Settings(database_url="postgres://u:p@db.supabase.co:6543/postgres")

    def test_accepts_session_pooler_port_5432(self):
        s = Settings(database_url="postgresql://u:p@db.supabase.co:5432/postgres")
        assert s.database_url == "postgresql://u:p@db.supabase.co:5432/postgres"

    def test_accepts_url_with_no_port(self):
        s = Settings(database_url="postgresql://u:p@localhost/db")
        assert s.database_url == "postgresql://u:p@localhost/db"

    def test_accepts_sqlite_url(self):
        s = Settings(database_url="sqlite+aiosqlite:///./test.db")
        assert s.database_url == "sqlite+aiosqlite:///./test.db"


class TestIsHardened:
    def test_development_is_not_hardened(self):
        s = Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db",
            environment="development",
        )
        assert s.is_hardened is False

    def test_test_environment_is_not_hardened(self):
        s = Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db",
            environment="test",
        )
        assert s.is_hardened is False

    def test_staging_is_hardened(self):
        s = Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db",
            environment="staging",
        )
        assert s.is_hardened is True

    def test_production_is_hardened(self):
        s = Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db",
            environment="production",
        )
        assert s.is_hardened is True


class TestCorsOriginsValidator:
    def test_accepts_valid_origins(self):
        s = Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db",
            cors_origins=["https://example.buffingchi.com", "http://localhost:8081"],
        )
        assert "https://example.buffingchi.com" in s.cors_origins

    def test_accepts_empty_origins(self):
        s = Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db",
            cors_origins=[],
        )
        assert s.cors_origins == []

    def test_rejects_wildcard(self):
        with pytest.raises(ValidationError, match="Wildcard"):
            Settings(
                database_url="postgresql+asyncpg://u:p@localhost/db",
                cors_origins=["*"],
            )

    def test_rejects_wildcard_mixed_with_valid(self):
        with pytest.raises(ValidationError, match="Wildcard"):
            Settings(
                database_url="postgresql+asyncpg://u:p@localhost/db",
                cors_origins=["https://example.com", "*"],
            )


class TestScanAiSettingsValidation:
    DB = "postgresql+asyncpg://u:p@localhost/db"

    @pytest.mark.parametrize("field", ["scan_model", "scan_enhanced_model"])
    def test_unknown_model_names_field(self, field):
        with pytest.raises(ValidationError, match=field.upper()):
            Settings(database_url=self.DB, **{field: "gpt-4.1-mini"})

    @pytest.mark.parametrize("value", [0, 16_385])
    def test_scan_max_tokens_out_of_range(self, value):
        with pytest.raises(ValidationError, match="scan_max_tokens"):
            Settings(database_url=self.DB, scan_max_tokens=value)

    @pytest.mark.parametrize("value", [0.5, 120.5])
    def test_scan_timeout_out_of_range(self, value):
        with pytest.raises(ValidationError, match="scan_timeout_seconds"):
            Settings(database_url=self.DB, scan_timeout_seconds=value)

    def test_bounds_and_allowed_models_accepted(self):
        s = Settings(
            database_url=self.DB,
            scan_model="gpt-4o-2024-08-06",
            scan_max_tokens=16_384,
            scan_timeout_seconds=1.0,
        )
        assert s.scan_model == "gpt-4o-2024-08-06"


class TestAnthropicBaseUrl:
    """AI_ANTHROPIC_BASE_URL (#539): unset by default; only a local host in any
    environment, and nothing at all in staging or production."""

    DB = "postgresql+asyncpg://u:p@localhost/db"

    def _settings(self, url: str | None, environment: str = "development"):
        kwargs = {} if url is None else {"ai_anthropic_base_url": url}
        return Settings(database_url=self.DB, environment=environment, **kwargs)

    def test_unset_by_default(self):
        assert self._settings(None).ai_anthropic_base_url == ""

    def test_the_generic_sdk_variable_is_not_read(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:1")
        assert self._settings(None).ai_anthropic_base_url == ""

    def test_read_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("AI_ANTHROPIC_BASE_URL", "http://127.0.0.1:18080")
        s = Settings(database_url=self.DB, environment="development")
        assert s.ai_anthropic_base_url == "http://127.0.0.1:18080"

    @pytest.mark.parametrize("environment", ["development", "test"])
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:18080",
            "http://localhost:18080",
            "http://[::1]:18080",
            "https://localhost",
            "http://localhost/anthropic",
        ],
    )
    def test_local_url_is_accepted_outside_hardened_environments(
        self, url, environment
    ):
        assert self._settings(url, environment).ai_anthropic_base_url == url

    @pytest.mark.parametrize("environment", ["development", "test"])
    @pytest.mark.parametrize(
        "url",
        [
            "https://gateway.example.com/anthropic",
            "https://api.anthropic.com",
            "http://127.0.0.1.evil.example.com",
            "http://localhost.evil.example.com",
            "http://10.0.0.5:8080",
            "http://0.0.0.0:8080",
            "http://localhost@evil.example.com",
        ],
    )
    def test_non_local_url_is_rejected_in_every_environment(self, url, environment):
        # ENVIRONMENT defaults to development: a deployment that forgot to set
        # it must not be able to redirect the API key and photos.
        with pytest.raises(ValidationError) as exc:
            self._settings(url, environment)
        assert "AI_ANTHROPIC_BASE_URL" in str(exc.value)

    @pytest.mark.parametrize("environment", ["staging", "production"])
    @pytest.mark.parametrize("url", ["", "   "])
    def test_unset_is_accepted_in_hardened_environments(self, url, environment):
        assert self._settings(url, environment).ai_anthropic_base_url == ""

    @pytest.mark.parametrize("environment", ["staging", "production"])
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:18080",
            "http://localhost:18080",
            "http://[::1]:18080",
            "https://evil.example.com",
        ],
    )
    def test_any_value_fails_boot_in_hardened_environments(self, url, environment):
        with pytest.raises(ValidationError) as exc:
            self._settings(url, environment)
        assert "AI_ANTHROPIC_BASE_URL" in str(exc.value)

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://127.0.0.1/x",
            "127.0.0.1:18080",
            "http://",
            "file:///etc/passwd",
            "http://user:secret@127.0.0.1:18080",
            "http://127.0.0.1:18080?x=1",
            "http://127.0.0.1:18080#frag",
            "http://[::1",
            "http://localhost:abc",
            "http://localhost:99999",
            "http://127.0.0.1:-1",
        ],
    )
    def test_malformed_url_is_rejected_everywhere(self, url):
        with pytest.raises(ValidationError) as exc:
            self._settings(url)
        assert "secret" not in str(exc.value)
        assert "AI_ANTHROPIC_BASE_URL" in str(exc.value)

    def test_the_value_is_never_echoed_in_the_error(self):
        for environment in ("development", "production"):
            with pytest.raises(ValidationError) as exc:
                self._settings("https://evil.example.com/token-abc123", environment)
            assert "abc123" not in str(exc.value)
        with pytest.raises(ValidationError) as exc:
            self._settings("http://localhost:abc123")
        assert "abc123" not in str(exc.value)


def test_blank_guard_source_get_field_value_is_an_inert_hook():
    """PydanticBaseSettingsSource requires get_field_value; the guard source
    contributes no values, so the hook must return "not found"."""
    from app.core.config import _BlankGuardSource

    source = _BlankGuardSource(Settings, None, ())
    assert source.get_field_value(None, "environment") == (None, "environment", False)

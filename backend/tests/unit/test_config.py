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

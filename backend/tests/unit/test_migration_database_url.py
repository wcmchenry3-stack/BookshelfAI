"""MIGRATION_DATABASE_URL selection for Alembic (#696).

The app connects with DATABASE_URL as the DML-only ``bookshelf_app`` role, so
Alembic needs the owner role's DSN. MIGRATION_DATABASE_URL carries it; when
unset or blank, migrations fall back to DATABASE_URL (local dev, CI).
"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings

_APP_URL = "postgresql://bookshelf_app:p@db.example:5432/postgres"
_OWNER_URL = "postgresql://postgres:p@db.example:5432/postgres"


@pytest.fixture(autouse=True)
def _no_ambient_migration_url(monkeypatch):
    monkeypatch.delenv("MIGRATION_DATABASE_URL", raising=False)


def test_defaults_to_empty():
    assert Settings(database_url=_APP_URL).migration_database_url == ""


def test_falls_back_to_database_url_when_unset():
    s = Settings(database_url=_APP_URL)
    assert (
        s.async_migration_database_url
        == "postgresql+asyncpg://bookshelf_app:p@db.example:5432/postgres"
    )


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_env_var_falls_back_to_database_url(monkeypatch, blank):
    monkeypatch.setenv("MIGRATION_DATABASE_URL", blank)
    s = Settings(database_url=_APP_URL)
    assert s.async_migration_database_url == s.async_database_url


def test_uses_migration_url_when_set():
    s = Settings(database_url=_APP_URL, migration_database_url=_OWNER_URL)
    assert (
        s.async_migration_database_url
        == "postgresql+asyncpg://postgres:p@db.example:5432/postgres"
    )
    # The app engine keeps using DATABASE_URL.
    assert (
        s.async_database_url
        == "postgresql+asyncpg://bookshelf_app:p@db.example:5432/postgres"
    )


def test_read_from_environment(monkeypatch):
    monkeypatch.setenv("MIGRATION_DATABASE_URL", _OWNER_URL)
    s = Settings(database_url=_APP_URL)
    assert s.async_migration_database_url.startswith("postgresql+asyncpg://postgres:")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgres://o:p@h/db", "postgresql+asyncpg://o:p@h/db"),
        ("postgresql://o:p@h/db", "postgresql+asyncpg://o:p@h/db"),
        ("postgresql+asyncpg://o:p@h/db", "postgresql+asyncpg://o:p@h/db"),
        ("sqlite:///./m.db", "sqlite+aiosqlite:///./m.db"),
    ],
)
def test_normalizes_scheme(url, expected):
    s = Settings(database_url=_APP_URL, migration_database_url=url)
    assert s.async_migration_database_url == expected


def test_rejects_transaction_pooler_port():
    with pytest.raises(ValidationError, match="MIGRATION_DATABASE_URL uses port 6543"):
        Settings(
            database_url=_APP_URL,
            migration_database_url="postgresql://postgres:p@db.example:6543/postgres",
        )


def test_invalid_url_error_names_the_setting_without_the_value():
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            database_url=_APP_URL,
            migration_database_url="postgresql://postgres:s3cret@db.example:notaport/x",
        )
    message = str(exc_info.value)
    assert "MIGRATION_DATABASE_URL is not a valid URL" in message
    assert "s3cret" not in message


def test_alembic_env_uses_the_migration_url():
    """alembic/env.py must read the migration DSN, not the app's."""
    from pathlib import Path

    env_py = Path(__file__).resolve().parents[2] / "alembic" / "env.py"
    source = env_py.read_text(encoding="utf-8")
    assert "settings.async_migration_database_url" in source
    assert "settings.async_database_url" not in source

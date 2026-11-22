"""The web e2e launcher's database isolation guard (#470).

The e2e backend writes ``ai_settings`` and deletes every ``scan_records`` row,
so it must refuse anything but a local, e2e-named database.
"""

import importlib.util
from pathlib import Path

import pytest

GUARD = (
    Path(__file__).resolve().parents[3] / "frontend" / "e2e" / "stack" / "db_guard.py"
)
spec = importlib.util.spec_from_file_location("e2e_db_guard", GUARD)
db_guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(db_guard)

check = db_guard.check_database_url
Unsafe = db_guard.UnsafeDatabaseError


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://test:test@localhost:5432/e2e",
        "postgresql+asyncpg://test:test@127.0.0.1:5433/bookshelf_e2e",
        "postgresql+asyncpg://test:test@[::1]:5432/e2efresh",
    ],
)
def test_local_e2e_database_is_allowed(url):
    check(url, "development")


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://u:p@db.example.com:5432/e2e",  # remote host
        "postgresql+asyncpg://u:p@localhost:5432/bookshelf",  # not an e2e name
        "postgresql+asyncpg://u:p@localhost:5432/postgres",
        "postgresql+asyncpg://u:p@db.abc.supabase.co:5432/e2e",
        "postgresql+asyncpg://u:p@localhost:6543/e2e?host=db.example.com",  # query
        "postgresql+asyncpg://u:p@dpg-x.render.com/e2e",
        "postgresql+asyncpg://u:p@aws-0.pooler.example.com/e2e",
        "not a url",
        "",
    ],
)
def test_other_databases_are_refused(url):
    with pytest.raises(Unsafe):
        check(url, "development")


@pytest.mark.parametrize("environment", ["staging", "production", " Production "])
def test_hardened_environment_is_refused(environment):
    with pytest.raises(Unsafe):
        check("postgresql+asyncpg://u:p@localhost:5432/e2e", environment)


def test_override_needs_the_exact_database_name():
    url = "postgresql+asyncpg://u:p@localhost:5432/scratch"
    with pytest.raises(Unsafe):
        check(url, "development")
    with pytest.raises(Unsafe):
        check(url, "development", allow_db="scratch2")
    check(url, "development", allow_db="scratch")


def test_override_never_allows_a_remote_or_hosted_database():
    with pytest.raises(Unsafe):
        check(
            "postgresql+asyncpg://u:p@db.example.com/scratch", "development", "scratch"
        )
    with pytest.raises(Unsafe):
        check("postgresql+asyncpg://u:p@localhost/scratch", "production", "scratch")


def test_message_does_not_echo_the_url():
    with pytest.raises(Unsafe) as err:
        check("postgresql+asyncpg://user:s3cret@db.example.com/e2e", "development")
    assert "s3cret" not in str(err.value)


def test_connected_database_name_is_checked_again():
    db_guard.check_server_identity("bookshelf_e2e")
    with pytest.raises(Unsafe):
        db_guard.check_server_identity("bookshelf")
    db_guard.check_server_identity("scratch", allow_db="scratch")


def test_enforce_exits_before_start_and_sets_the_flag(monkeypatch):
    monkeypatch.setattr(db_guard, "_passed", False)
    with pytest.raises(SystemExit):
        db_guard.enforce(
            "postgresql+asyncpg://u:p@db.example.com/e2e", "development", None
        )
    assert db_guard.passed() is False
    db_guard.enforce("postgresql+asyncpg://u:p@localhost/e2e", "development", None)
    assert db_guard.passed() is True

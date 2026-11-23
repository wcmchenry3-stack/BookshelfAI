"""Settings for the account export (#694): Supabase Storage, the bucket,
the sender address and the Resend key.

Defaults keep the feature off (no URL, no keys). The URL must be https (http
only for a local host) with nothing but a host; the bucket must be a bucket
id; ``EMAIL_FROM`` follows ``EMAIL_REPLY_TO``'s rules; secrets are stripped
and never echoed in a validation error.
"""

import uuid

import pytest
from pydantic import ValidationError

from app.core.config import Settings

_DB = "postgresql+asyncpg://u:p@localhost/db"
KEY = f"k-{uuid.uuid5(uuid.NAMESPACE_URL, 'export-settings')}"
_ENV = (
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
    "EXPORT_BUCKET",
    "EMAIL_FROM",
    "RESEND_API_KEY",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, database_url=_DB, **kw)


def test_defaults_leave_the_export_off():
    s = _settings()
    assert s.supabase_url == ""
    assert s.supabase_service_role_key == ""
    assert s.resend_api_key == ""
    assert s.export_bucket == "account-exports"
    assert s.email_from == "noreply@mail.buffingchi.com"


def test_read_from_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://ref.supabase.co/")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", f" {KEY} ")
    monkeypatch.setenv("RESEND_API_KEY", f"{KEY}\n")
    monkeypatch.setenv("EXPORT_BUCKET", "exports-dev")
    monkeypatch.setenv("EMAIL_FROM", "hello@mail.example.com")
    s = _settings()
    assert s.supabase_url == "https://ref.supabase.co"
    assert s.supabase_service_role_key == KEY
    assert s.resend_api_key == KEY
    assert s.export_bucket == "exports-dev"
    assert s.email_from == "hello@mail.example.com"


@pytest.mark.parametrize(
    "url",
    [
        "https://ref.supabase.co",
        "https://ref.supabase.co:8443",
        "http://localhost:54321",
    ],
)
def test_accepts_a_plain_https_url(url):
    assert _settings(supabase_url=url).supabase_url == url


@pytest.mark.parametrize(
    "url",
    [
        "http://ref.supabase.co",
        "ftp://ref.supabase.co",
        "https://user:pw@ref.supabase.co",
        "https://ref.supabase.co/storage/v1",
        "https://ref.supabase.co?x=1",
        "https://ref.supabase.co#f",
        "https://ref.supabase.co:99999",
        "https://",
        "ref.supabase.co",
    ],
)
def test_rejects_anything_else_without_echoing_it(url):
    with pytest.raises(ValidationError) as exc:
        _settings(supabase_url=url)
    assert "input_value" not in str(exc.value)


@pytest.mark.parametrize("bucket", ["", "A", "Upper-Case", "has space", "-x", "x/y"])
def test_rejects_a_bad_bucket(bucket):
    with pytest.raises(ValidationError):
        _settings(export_bucket=bucket)


@pytest.mark.parametrize("bad", ["nope", "Bookshelf <a@b.com>", "a@b.com, c@d.com"])
def test_rejects_a_bad_from_address(bad):
    with pytest.raises(ValidationError) as exc:
        _settings(email_from=bad)
    assert "EMAIL_FROM" in str(exc.value)

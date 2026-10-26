"""A blank env var for a setting with a default means "not set" (#648).

Exceptions that stay hard errors: DATABASE_URL (no default), ENVIRONMENT and
TRUSTED_HOSTS (a default would change security posture), and JWT_PRIVATE_KEY in
a hardened environment (catalog_token.check_configured).

Settings are built with ``Settings(_env_file=None)`` under monkeypatch; no
module is reloaded (see test_environment_hardening.py and #520).
"""

import subprocess
import sys

import pytest
from pydantic import ValidationError

from app.core import catalog_token
from app.core.config import BlankSettingError, Settings

_MANAGED = (
    "ENVIRONMENT",
    "TRUSTED_HOSTS",
    "SCAN_MODEL",
    "SCAN_ENHANCED_MODEL",
    "SCAN_MAX_TOKENS",
    "SCAN_TIMEOUT_SECONDS",
    "DATABASE_URL",
    "JWT_PRIVATE_KEY",
    "TURNSTILE_REQUIRED",
    "TURNSTILE_SECRET_KEY",
)


@pytest.fixture
def env(monkeypatch):
    for name in _MANAGED:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/db")
    return monkeypatch


@pytest.mark.security
@pytest.mark.parametrize(
    "name,attr,default",
    [
        ("SCAN_MODEL", "scan_model", "gpt-4o-mini"),
        ("SCAN_ENHANCED_MODEL", "scan_enhanced_model", "gpt-4o"),
        ("SCAN_MAX_TOKENS", "scan_max_tokens", 2048),
        ("SCAN_TIMEOUT_SECONDS", "scan_timeout_seconds", 45.0),
    ],
)
def test_blank_scan_setting_gives_default(env, name, attr, default):
    env.setenv(name, "")
    assert getattr(Settings(_env_file=None), attr) == default


def test_blank_in_env_file_gives_default(env, tmp_path):
    f = tmp_path / ".env"
    f.write_text("SCAN_MODEL=\nSCAN_MAX_TOKENS=\n")
    s = Settings(_env_file=str(f))
    assert s.scan_model == "gpt-4o-mini"
    assert s.scan_max_tokens == 2048


@pytest.mark.security
def test_blank_database_url_still_errors(env):
    env.setenv("DATABASE_URL", "")
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)
    assert "database_url" in str(exc.value)


@pytest.mark.security
def test_blank_jwt_key_in_production_still_fails_hardened_check(env):
    env.setenv("ENVIRONMENT", "production")
    env.setenv("JWT_PRIVATE_KEY", "")
    cfg = Settings(_env_file=None)
    with pytest.raises(catalog_token.CatalogTokenConfigError):
        catalog_token.check_configured(cfg)


@pytest.mark.security
def test_invalid_non_blank_value_still_fails(env):
    env.setenv("SCAN_MODEL", "bogus")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.security
def test_two_bad_values_are_both_named_and_secret_hidden(env):
    env.setenv("SCAN_MODEL", "bogus")
    env.setenv("SCAN_MAX_TOKENS", "abc")
    env.setenv("ORIGIN_AUTH_SECRET", "short-secret-value")
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)
    text = str(exc.value)
    assert "scan_model" in text and "scan_max_tokens" in text
    assert "SCAN_MODEL" in text
    assert "short-secret-value" not in text


@pytest.mark.security
@pytest.mark.parametrize("name", ["ENVIRONMENT", "TRUSTED_HOSTS"])
@pytest.mark.parametrize("value", ["", "   "])
def test_blank_environment_and_trusted_hosts_are_boot_errors(env, name, value):
    env.setenv(name, value)
    with pytest.raises(BlankSettingError, match=name):
        Settings(_env_file=None)


@pytest.mark.security
@pytest.mark.parametrize("name", ["ENVIRONMENT", "TRUSTED_HOSTS"])
def test_blank_in_env_file_is_boot_error(env, tmp_path, name):
    f = tmp_path / ".env"
    f.write_text(f"{name}=\n")
    with pytest.raises(BlankSettingError, match=name):
        Settings(_env_file=str(f))


def test_unset_environment_and_trusted_hosts_keep_defaults(env):
    s = Settings(_env_file=None)
    assert s.environment == "development"
    assert s.trusted_hosts == ["*"]


# --- python -m app.core.config_check --------------------------------------


def _run_check(env_vars: dict[str, str]):
    import os

    clean = {
        k: v
        for k, v in os.environ.items()
        if k.upper() not in _MANAGED and k.upper() != "ORIGIN_AUTH_SECRET"
    }
    clean.update(env_vars)
    return subprocess.run(
        [sys.executable, "-m", "app.core.config_check"],
        capture_output=True,
        text=True,
        env=clean,
        cwd=_backend_dir(),
        check=False,
    )


def _backend_dir() -> str:
    from pathlib import Path

    return str(Path(__file__).resolve().parents[2])


_DB = "postgresql://u:p@localhost:5432/db"


def test_check_ok_on_valid_environment():
    r = _run_check(
        {"DATABASE_URL": _DB, "SCAN_MODEL": "", "TURNSTILE_REQUIRED": "false"}
    )
    assert r.returncode == 0
    assert r.stdout.strip() == "OK"


@pytest.mark.security
def test_check_fails_on_invalid_environment_and_prints_no_values():
    secret = "super-secret-short-ABC"
    r = _run_check(
        {
            "DATABASE_URL": _DB,
            "SCAN_MODEL": "bogus-model-name",
            "ORIGIN_AUTH_SECRET": secret,
            "ENVIRONMENT": "production",
        }
    )
    assert r.returncode == 1
    out = r.stdout + r.stderr
    assert "SCAN_MODEL" in out
    assert "ORIGIN_AUTH_SECRET" in out
    assert "bogus-model-name" not in out
    assert secret not in out
    assert "postgresql://" not in out


@pytest.mark.security
def test_check_fails_on_blank_environment():
    r = _run_check({"DATABASE_URL": _DB, "ENVIRONMENT": ""})
    assert r.returncode == 1
    assert "ENVIRONMENT" in r.stdout


@pytest.mark.security
def test_check_fails_on_missing_jwt_key_in_production():
    r = _run_check(
        {
            "DATABASE_URL": _DB,
            "ENVIRONMENT": "production",
            "TURNSTILE_REQUIRED": "false",
        }
    )
    assert r.returncode == 1
    assert "JWT_PRIVATE_KEY" in r.stdout


@pytest.mark.security
def test_check_fails_when_turnstile_required_without_secret():
    r = _run_check({"DATABASE_URL": _DB, "TURNSTILE_REQUIRED": "true"})
    assert r.returncode == 1
    assert "TURNSTILE_SECRET_KEY" in r.stdout


def test_check_ok_when_turnstile_not_required():
    r = _run_check({"DATABASE_URL": _DB, "TURNSTILE_REQUIRED": "false"})
    assert r.returncode == 0


def test_check_ok_when_turnstile_required_and_secret_set():
    r = _run_check(
        {
            "DATABASE_URL": _DB,
            "TURNSTILE_REQUIRED": "true",
            "TURNSTILE_SECRET_KEY": "x",
        }
    )
    assert r.returncode == 0


# --- precedence of the blank guard ------------------------------------------


@pytest.mark.security
def test_blank_in_dotenv_but_set_in_process_env_boots(env, tmp_path):
    f = tmp_path / ".env"
    f.write_text("ENVIRONMENT=\n")
    env.setenv("ENVIRONMENT", "production")
    assert Settings(_env_file=str(f)).environment == "production"


@pytest.mark.security
def test_blank_in_process_env_but_set_in_dotenv_raises(env, tmp_path):
    f = tmp_path / ".env"
    f.write_text("ENVIRONMENT=production\n")
    env.setenv("ENVIRONMENT", "")
    with pytest.raises(BlankSettingError, match="ENVIRONMENT"):
        Settings(_env_file=str(f))


@pytest.mark.security
def test_later_dotenv_file_overrides_earlier(env, tmp_path):
    blank = tmp_path / "blank.env"
    blank.write_text("ENVIRONMENT=\n")
    full = tmp_path / "full.env"
    full.write_text("ENVIRONMENT=production\n")
    assert Settings(_env_file=(str(blank), str(full))).environment == "production"
    with pytest.raises(BlankSettingError, match="ENVIRONMENT"):
        Settings(_env_file=(str(full), str(blank)))


@pytest.mark.security
def test_init_kwarg_overrides_blank_env(env):
    env.setenv("ENVIRONMENT", "")
    assert Settings(_env_file=None, environment="staging").environment == "staging"

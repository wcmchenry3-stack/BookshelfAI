"""#540: the legacy daily scan counters are gone from the app, and the env
vars that fed them no longer break boot."""

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings

APP = Path(__file__).resolve().parents[2] / "app"
_DB = "postgresql+asyncpg://u:p@localhost/db"

# `scan_quota` as a module or identifier; the gateway's `_reserve_scan_quota`
# and `_refund_scan_quota` in app/api/scan.py are different names and stay.
REMOVED = re.compile(
    r"scan_usage_daily|ScanUsageDaily|purge_old_scan_usage"
    r"|enhanced_scan_credits|enhanced_scan_free_credits"
    r"|scan_daily_limit_per_user"
    r"|(?<![A-Za-z0-9_])scan_quota(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


def test_no_reference_to_the_removed_scan_counters_in_app():
    hits = [
        f"{path.relative_to(APP)}:{n}: {line.strip()}"
        for path in APP.rglob("*.py")
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if REMOVED.search(line)
    ]
    assert hits == []


def test_removed_module_and_model_cannot_be_imported():
    with pytest.raises(ImportError):
        import app.services.scan_quota
    with pytest.raises(ImportError):
        import app.models.scan_usage  # noqa: F401


@pytest.mark.parametrize(
    "name", ["ENHANCED_SCAN_FREE_CREDITS", "SCAN_DAILY_LIMIT_PER_USER"]
)
def test_removed_process_env_var_does_not_break_boot(monkeypatch, name):
    """The Render case: a process env var that matches no field is ignored."""
    monkeypatch.setenv(name, "7")
    s = Settings(database_url=_DB, _env_file=None)
    assert not hasattr(s, name.lower())


@pytest.mark.parametrize(
    "name", ["ENHANCED_SCAN_FREE_CREDITS", "SCAN_DAILY_LIMIT_PER_USER"]
)
def test_removed_key_in_the_env_file_is_refused(tmp_path, name):
    """Documented upgrade note (docs/deployment.md): a local .env must drop it."""
    env_file = tmp_path / ".env"
    env_file.write_text(f"{name}=7\n")
    with pytest.raises(ValidationError):
        Settings(database_url=_DB, _env_file=env_file)

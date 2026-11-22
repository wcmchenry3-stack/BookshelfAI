"""The database size alert script trips at 70 percent of the limit (#673)."""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "db" / "check_size_threshold.sh"
SQL = ROOT / "scripts" / "db" / "db_size_report.sql"
WORKFLOW = ROOT / ".github" / "workflows" / "db-backup.yml"
GB = 1024**3


def _run(*args: object) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(SCRIPT), *map(str, args)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_below_threshold_passes():
    assert _run(int(5.5 * GB), 8).returncode == 0


def test_exactly_at_threshold_fails():
    # 70 percent of 10 GB is exactly 7 GB.
    assert _run(7 * GB, 10).returncode == 1
    assert _run(7 * GB - 1, 10).returncode == 0


def test_above_limit_fails():
    assert _run(9 * GB, 8).returncode == 1


def test_custom_percent():
    assert _run(4 * GB, 8, 50).returncode == 1
    assert _run(4 * GB - 1, 8, 50).returncode == 0


def test_output_leaks_no_numbers():
    for size in (123456, 7 * GB):
        out = _run(size, 10).stdout
        assert str(size) not in out
        assert "GB" not in out


@pytest.mark.parametrize(
    "args",
    [(), ("abc", 8), (1, 0), (1, 8, 0), (1, 8, 101), (-1, 8), (1, "x")],
)
def test_bad_args_exit_2(args):
    assert _run(*args).returncode == 2


def test_sql_report_is_read_only():
    sql = SQL.read_text().lower()
    for word in (
        "insert ",
        "update ",
        "delete ",
        "drop ",
        "alter ",
        "truncate",
        "create ",
    ):
        assert word not in sql
    assert "pg_database_size" in sql and "scan_records" in sql


def test_workflow_wires_the_alert():
    text = WORKFLOW.read_text()
    assert "check_size_threshold.sh" in text
    assert "DB_SIZE_LIMIT_GB" in text

"""The database size alert script trips at 70 percent of the limit (#673)."""

import re
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


def test_zero_size_is_below_threshold():
    assert _run(0, 8).returncode == 0


@pytest.mark.parametrize(
    "args",
    [
        ("100", "08"),  # leading zero: bash would parse octal and crash
        ("0100", "8"),
        ("100", "8", "070"),
        ("00", "8"),
        ("9" * 16, "8"),  # over-long size
        ("100", "9" * 7),  # over-long limit
        ("100", "8", "1" + "0" * 20),
        ("9" * 40, "9" * 40, "9" * 40),
    ],
)
def test_leading_zero_and_overlong_inputs_exit_2(args):
    assert _run(*args).returncode == 2


def _statements(sql: str) -> list[str]:
    no_block = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    no_line = re.sub(r"--[^\n]*", "", no_block)
    return [x.strip() for x in no_line.split(";") if x.strip()]


FORBIDDEN = (
    "insert",
    "update",
    "delete",
    "merge",
    "copy",
    "create",
    "alter",
    "drop",
    "truncate",
    "grant",
    "revoke",
    "vacuum",
    "call",
    "set",
    "for update",
)


def test_sql_report_is_read_only():
    statements = _statements(SQL.read_text())
    assert len(statements) == 3
    for stmt in statements:
        low = stmt.lower()
        assert re.match(r"(select|with)\b", low), stmt
        for word in FORBIDDEN:
            assert not re.search(rf"\b{word}\b", low), (word, stmt)
    text = SQL.read_text().lower()
    assert "pg_database_size" in text and "scan_records" in text


def test_sql_read_only_check_catches_writes():
    for bad in (
        "DELETE FROM x",
        "select 1; drop table x",
        "SELECT * FROM t FOR UPDATE",
    ):
        stmts = _statements(bad)
        assert any(
            not re.match(r"(select|with)\b", x.lower())
            or any(re.search(rf"\b{w}\b", x.lower()) for w in FORBIDDEN)
            for x in stmts
        )


def test_workflow_wires_the_alert():
    text = WORKFLOW.read_text()
    assert "check_size_threshold.sh" in text
    assert "DB_SIZE_LIMIT_GB" in text

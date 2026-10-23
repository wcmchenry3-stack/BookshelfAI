"""Regression guard for the CI coverage gate (#621).

CI's test-python job runs ``pytest --cov=. --cov-fail-under=<threshold>`` from
``backend/``. pytest-cov rounds the total to coverage's ``precision`` before
comparing it with the threshold, and coverage's default precision is 0. So a
total of 79.72% rounded to 80 and passed an 80% gate (exit 0), while the
summary line, which compares the unrounded total, printed "FAIL Required test
coverage of 80% not reached". ``backend/.coveragerc`` sets ``precision = 2``.

These tests run a tiny project in a subprocess with the real
``backend/.coveragerc`` and a total that sits inside the old rounding window,
and assert the gate fails with a non-zero exit.
"""

import configparser
import os
import shutil
import subprocess
import sys
from pathlib import Path

from coverage.results import should_fail_under

BACKEND = Path(__file__).resolve().parents[2]
COVERAGERC = BACKEND / ".coveragerc"

# 68 executed assignments + an executed ``def`` + one unexecuted ``return``:
# 69 of 70 statements covered = 98.571...%. Rounded to precision 0 that is 99.
_COVERED_ASSIGNMENTS = 68
_STATEMENTS = _COVERED_ASSIGNMENTS + 2
_TOTAL = 100.0 * (_STATEMENTS - 1) / _STATEMENTS
_THRESHOLD = 99


def _write_project(root: Path) -> None:
    shutil.copy(COVERAGERC, root / ".coveragerc")
    # An ini file stops pytest walking up and picking up backend/'s config.
    (root / "pytest.ini").write_text("[pytest]\n")
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    body = "".join(f"x{i} = {i}\n" for i in range(_COVERED_ASSIGNMENTS))
    body += "\n\ndef uncovered():\n    return 1\n"
    (pkg / "mod.py").write_text(body)
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_mod.py").write_text(
        "import pkg.mod\n\n\ndef test_import():\n    assert pkg.mod.x0 == 0\n"
    )


def _run_gate(root: Path, *extra: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("COV")}
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:randomly",
            "--cov=pkg",
            f"--cov-fail-under={_THRESHOLD}",
            "-q",
            *extra,
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_fixture_total_is_inside_the_old_rounding_window():
    # Calibration: with precision 0 this total would have passed the gate,
    # with the configured precision it must fail.
    assert should_fail_under(_TOTAL, _THRESHOLD, 0) is False
    assert should_fail_under(_TOTAL, _THRESHOLD, 2) is True


def test_coveragerc_sets_precision_and_omits_non_app_code():
    cfg = configparser.ConfigParser()
    cfg.read(COVERAGERC)
    assert cfg.getint("report", "precision") >= 2
    omit = cfg.get("run", "omit").split()
    assert "tests/*" in omit
    assert "alembic/*" in omit


def test_coverage_below_threshold_exits_nonzero(tmp_path):
    _write_project(tmp_path)

    result = _run_gate(tmp_path)

    output = result.stdout + result.stderr
    assert "98.57%" in output, output
    assert result.returncode != 0, (
        "coverage below --cov-fail-under must fail the run\n" + output
    )


def test_coverage_at_threshold_exits_zero(tmp_path):
    _write_project(tmp_path)

    result = _run_gate(tmp_path, f"--cov-fail-under={_TOTAL - 1:.2f}")

    assert result.returncode == 0, result.stdout + result.stderr

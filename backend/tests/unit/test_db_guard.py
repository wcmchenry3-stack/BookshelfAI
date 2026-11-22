"""The REQUIRE_DB_TESTS guard (#760): no real DB + required = error, not skip."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests import _db_guard

REAL = {"DATABASE_URL": "postgresql://test:test@localhost:5432/test"}
BACKEND = Path(__file__).resolve().parents[2]


def test_has_real_db():
    assert _db_guard.has_real_db(REAL)
    assert not _db_guard.has_real_db({})
    assert not _db_guard.has_real_db({"DATABASE_URL": ""})
    assert not _db_guard.has_real_db({"DATABASE_URL": _db_guard.DUMMY_DB_URL})


@pytest.mark.parametrize("flag", ["1", "true", "TRUE", "yes"])
def test_required_without_real_db_is_an_error(flag):
    env = {"REQUIRE_DB_TESTS": flag, "DATABASE_URL": _db_guard.DUMMY_DB_URL}
    assert "REQUIRE_DB_TESTS" in _db_guard.missing_db_error(env)
    assert _db_guard.missing_db_error({"REQUIRE_DB_TESTS": flag})


def test_required_with_real_db_is_fine():
    assert _db_guard.missing_db_error({"REQUIRE_DB_TESTS": "1", **REAL}) is None


@pytest.mark.parametrize(
    "env", [{}, {"REQUIRE_DB_TESTS": "0"}, {"REQUIRE_DB_TESTS": ""}]
)
def test_not_required_never_errors(env):
    assert _db_guard.missing_db_error(env) is None


def test_conftest_aborts_collection_when_required_and_no_db():
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    env["REQUIRE_DB_TESTS"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/integration", "--collect-only", "-q"],
        cwd=BACKEND,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "REQUIRE_DB_TESTS" in proc.stdout + proc.stderr

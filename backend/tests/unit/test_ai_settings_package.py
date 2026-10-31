"""The ``app.ai.settings`` package facade (#807).

settings.py (the AI dials, kill switches and their CLI) was split into a
package. Callers and tests keep using ``from app.ai import settings as
ai_settings`` and ``ai_settings.<name>``, and many tests patch
``ai_settings.<name>``. These tests guard that, and that the behaviour is
byte-identical to the single module (a snapshot taken from it before the
split: ``tests/fixtures/ai_settings_snapshot.json``)."""

import ast
import asyncio
import inspect
import json
import logging
import os
import runpy
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai import settings as ai_settings
from app.ai.settings import cache as settings_cache
from app.ai.settings import errors as settings_errors
from app.ai.settings import view as settings_view
from tests.unit import _ai_settings_snapshot as snap

PACKAGE_DIR = Path(ai_settings.__file__).resolve().parent
BACKEND_DIR = Path(__file__).resolve().parents[2]
SNAPSHOT = BACKEND_DIR / "tests" / "fixtures" / "ai_settings_snapshot.json"

# Every top-level name app/ai/settings.py had before the split (stdlib,
# typing and SQLAlchemy imports aside), read from its AST on dev. Dropping
# one breaks a caller or a patch.
OLD_NAMES = [
    "AISetting",
    "AISettings",
    "AISettingsUnavailableError",
    "AISettingValueError",
    "ALLOWED_MODELS",
    "AREA_RULES",
    "DAILY_ALLOWANCE_BOUNDS",
    "DEFAULT_CLI_ACTOR",
    "DEFAULT_SCAN_RECORDS_ENABLED",
    "DEFAULT_SCAN_RECORDS_MAX_PER_USER",
    "DEFAULT_SCAN_RECORDS_RETENTION_DAYS",
    "DEFAULT_SCAN_REQUESTS_PER_USER_DAILY",
    "DEFAULT_ZERO_RESULT_ACTION",
    "DEFAULT_ZERO_RESULT_THRESHOLD",
    "DEFAULT_ZERO_RESULT_WINDOW_HOURS",
    "DEFINITIONS",
    "FREE_DAILY_CEILING_KEY",
    "FREE_DAILY_CEILING_MICROS_BOUNDS",
    "FeatureSpec",
    "FeatureSpecError",
    "GLOBAL_DAILY_LIMIT_BOUNDS",
    "KNOWN_KEYS",
    "MASTER_SWITCH",
    "MAX_IMAGE_PIXELS_BOUNDS",
    "MAX_MODEL_NAME_LENGTH",
    "MAX_UPDATED_BY_LENGTH",
    "OTHER_AREA",
    "QUOTA_TIERS",
    "RETIRED_KEYS",
    "SCAN_FEATURES",
    "SCAN_RECORDS_ENABLED_KEY",
    "SCAN_RECORDS_MAX_PER_USER_BOUNDS",
    "SCAN_RECORDS_MAX_PER_USER_KEY",
    "SCAN_RECORDS_RETENTION_DAYS_BOUNDS",
    "SCAN_RECORDS_RETENTION_DAYS_KEY",
    "SCAN_REQUESTS_PER_USER_DAILY_BOUNDS",
    "SCAN_REQUESTS_PER_USER_DAILY_KEY",
    "SEED_ACTOR",
    "SEED_TIMEOUT_SECONDS",
    "ScanUnavailableError",
    "SettingDefinition",
    "ZERO_RESULT_ACTIONS",
    "ZERO_RESULT_ACTION_CHARGE",
    "ZERO_RESULT_ACTION_KEY",
    "ZERO_RESULT_ACTION_THROTTLE",
    "ZERO_RESULT_THRESHOLD_BOUNDS",
    "ZERO_RESULT_THRESHOLD_KEY",
    "ZERO_RESULT_WINDOW_HOURS_BOUNDS",
    "ZERO_RESULT_WINDOW_HOURS_KEY",
    "_ALL_MODELS",
    "_BOOL_BOUNDS",
    "_CacheEntry",
    "_bool",
    "_bounded_int",
    "_cache",
    "_clock",
    "_daily_allowance",
    "_default_and_bounds",
    "_definitions",
    "_describe",
    "_dispose_engine",
    "_free_daily_ceiling",
    "_global_daily_limit",
    "_is_switch",
    "_load_rows",
    "_max_image_pixels",
    "_model",
    "_open_session",
    "_pair_problems",
    "_printable",
    "_provider",
    "_range",
    "_run",
    "_sanitise_rows",
    "_scan_records_max_per_user",
    "_scan_records_retention_days",
    "_scan_requests_per_user_daily",
    "_upsert",
    "_validate_actor",
    "_warn_ignored",
    "_warn_switch_disabled",
    "_with_carry_over",
    "_zero_result_action",
    "_zero_result_threshold",
    "_zero_result_window_hours",
    "area_of",
    "carried_over",
    "database",
    "default_model",
    "found_key",
    "get_ai_settings",
    "get_feature",
    "keys_by_area",
    "legacy_daily_key",
    "logger",
    "main",
    "no_match_key",
    "parse_cli_value",
    "read_rows",
    "reset_cache",
    "seed_ai_settings",
    "seed_ai_settings_at_startup",
    "seed_values",
    "sentry_sdk",
    "set_setting",
    "settings",
    "switch_provider",
    "validate_switch",
    "validate_value",
    "validate_write",
]


def _submodules() -> list[Path]:
    return sorted(p for p in PACKAGE_DIR.glob("*.py") if p.name != "__init__.py")


def _patchable() -> set[str]:
    """Facade names a module must reach through the package at call time:
    everything but classes (and exceptions), which are imported directly."""
    return {
        name
        for name in ai_settings.__all__
        if not inspect.isclass(getattr(ai_settings, name))
    }


def _bare_uses(source: str, patchable: set[str]) -> list[str]:
    """Bare-name reads of a patchable facade name inside a function or
    lambda (code that runs after import, where a patch must reach it)."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Name)
                and isinstance(inner.ctx, ast.Load)
                and inner.id in patchable
            ):
                found.append(f"{inner.lineno} {inner.id}")
    return found


# --- the facade ------------------------------------------------------------


def test_the_package_exposes_every_name_settings_py_had():
    missing = [name for name in OLD_NAMES if not hasattr(ai_settings, name)]
    assert missing == []
    assert sorted(ai_settings.__all__) == sorted(OLD_NAMES)


def test_every_package_function_is_re_exported():
    defined = set()
    for path in _submodules():
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
    assert defined - set(ai_settings.__all__) == set()


def test_no_submodule_shadows_an_exported_name():
    """A submodule named like an exported attribute would replace it on the
    package when imported."""
    clashes = {p.stem for p in _submodules()} & set(ai_settings.__all__)
    assert clashes == set()
    assert ai_settings.settings.__class__.__name__ == "Settings"
    assert inspect.isfunction(ai_settings.main)


def test_classes_and_errors_are_one_object_each():
    assert ai_settings.AISettings is settings_view.AISettings
    assert ai_settings.AISettingValueError is settings_errors.AISettingValueError
    assert issubclass(
        ai_settings.AISettingsUnavailableError, ai_settings.ScanUnavailableError
    )
    assert ai_settings._CacheEntry is settings_cache._CacheEntry


def test_every_module_logs_under_the_old_logger_name():
    assert ai_settings.logger is logging.getLogger("app.ai.settings")
    for path in _submodules():
        assert "getLogger" not in path.read_text(encoding="utf-8"), path.name


def test_every_module_stays_under_600_lines():
    for path in [*_submodules(), PACKAGE_DIR / "__init__.py"]:
        assert len(path.read_text(encoding="utf-8").splitlines()) < 600, path.name


# --- calls go through the package ------------------------------------------


def test_no_module_reads_a_patchable_name_bare():
    """A bare-name call would skip a patch on ``ai_settings.<name>``: every
    function, tunable, module object and the cache state is read through the
    package at call time."""
    patchable = _patchable()
    direct = [
        f"{path.name}:{use}"
        for path in _submodules()
        for use in _bare_uses(path.read_text(encoding="utf-8"), patchable)
    ]
    assert direct == []


def test_the_bare_name_guard_catches_a_bare_call():
    source = (
        "from app.ai.settings.store import read_rows\n"
        "async def f(db):\n"
        "    return await read_rows(db)\n"
        "g = lambda: DEFAULT_SCAN_REQUESTS_PER_USER_DAILY\n"
    )
    assert _bare_uses(source, _patchable()) == [
        "3 read_rows",
        "4 DEFAULT_SCAN_REQUESTS_PER_USER_DAILY",
    ]


def test_a_patch_on_the_package_reaches_sibling_modules():
    # definitions.py's defaults read the package's tunables and registry.
    with patch.object(ai_settings, "DEFAULT_SCAN_REQUESTS_PER_USER_DAILY", 7):
        assert ai_settings.AISettings({}).scan_requests_per_user_daily == 7
    fake = SimpleNamespace(provider="openai", model="gpt-4o")
    with patch.object(ai_settings, "get_feature", lambda name: fake):
        assert ai_settings.DEFINITIONS["scan.standard.model"].default() == "gpt-4o"
    # cache.py calls validation.py's _sanitise_rows and the package clock.
    with (
        patch.object(ai_settings, "_sanitise_rows", lambda rows: {"k": 1}),
        patch.object(ai_settings, "_clock", lambda: 123.0),
    ):
        ai_settings.reset_cache({"anything": 0})
        assert dict(ai_settings._cache.rows) == {"k": 1}
        assert ai_settings._cache.loaded_at == 123.0
    ai_settings.reset_cache()
    assert ai_settings._cache is None
    # validation.py warns through the package's sentry_sdk and logger.
    sentry, log = MagicMock(), MagicMock()
    with (
        patch.object(ai_settings, "sentry_sdk", sentry),
        patch.object(ai_settings, "logger", log),
    ):
        ai_settings._sanitise_rows({"scan.max_image_pixels": 1})
    sentry.capture_message.assert_called_once()
    log.warning.assert_called_once()
    # cli.py calls store.py through the package.
    with (
        patch.object(ai_settings, "validate_switch", MagicMock(side_effect=KeyError)),
        pytest.raises(KeyError),
    ):
        ai_settings.main(["switch", "scan.standard", "anthropic"])


async def test_a_patched_session_and_reader_reach_the_store():
    db = AsyncMock()
    with patch.object(ai_settings, "read_rows", AsyncMock(return_value={})) as rr:
        await ai_settings.set_setting(
            db, "scan.max_image_pixels", 2_000_000, updated_by="t"
        )
    rr.assert_awaited_once_with(db)

    async def _slow_seed(_db):
        await asyncio.sleep(5)

    @asynccontextmanager
    async def _session():
        yield db

    log = MagicMock()
    with (
        patch.object(ai_settings, "SEED_TIMEOUT_SECONDS", 0.01),
        patch.object(ai_settings, "_open_session", _session),
        patch.object(ai_settings, "seed_ai_settings", _slow_seed),
        patch.object(ai_settings, "logger", log),
    ):
        await ai_settings.seed_ai_settings_at_startup()
    assert log.warning.call_args.args[1] == "TimeoutError"


async def test_the_cache_state_lives_once_on_the_package():
    db = MagicMock()
    with (
        patch.object(ai_settings, "_load_rows", AsyncMock(return_value={})) as load,
        patch.object(ai_settings, "_clock", lambda: 1_000.0),
    ):
        ai_settings.reset_cache()
        await ai_settings.get_ai_settings(db)
        await ai_settings.get_ai_settings(db)
    load.assert_awaited_once()
    assert ai_settings._cache.loaded_at == 1_000.0
    assert "_cache" not in vars(settings_cache)
    ai_settings.reset_cache()


# --- behaviour identical to the single module ------------------------------


@pytest.fixture
def snapshot_env(monkeypatch):
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("USER", "snapshot-user")
    ai_settings.reset_cache()
    yield
    ai_settings.reset_cache()


def _roundtrip(value):
    return json.loads(json.dumps(value))


@pytest.fixture(scope="module")
def expected():
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def test_definitions_match_the_single_module(snapshot_env, expected):
    """Keys, order, descriptions, bounds, defaults and every validator's
    answer to the probes are as they were on dev."""
    assert _roundtrip(snap.definitions_snapshot()) == expected["definitions"]
    assert [d["key"] for d in expected["definitions"]] == list(ai_settings.KNOWN_KEYS)


def test_constants_and_areas_match_the_single_module(snapshot_env, expected):
    assert _roundtrip(snap.constants_snapshot()) == expected["constants"]
    assert _roundtrip(ai_settings.keys_by_area()) == expected["keys_by_area"]


def test_cli_output_matches_the_single_module(snapshot_env, expected):
    """Every case's exit code, stdout, stderr, log lines (logger name
    included) and Sentry messages are byte-identical."""
    got = _roundtrip(snap.cli_snapshot())
    assert [c["argv"] for c in got] == [c["argv"] for c in expected["cli"]]
    for case, want in zip(got, expected["cli"], strict=True):
        assert case == want, case["argv"]


def test_python_m_runs_the_cli_in_process(snapshot_env, capsys):
    with (
        patch.object(sys, "argv", ["app.ai.settings", "--help"]),
        pytest.raises(SystemExit) as exit_info,
    ):
        runpy.run_module("app.ai.settings", run_name="__main__")
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.startswith(
        "usage: python -m app.ai.settings [-h] {set,get,list,switch} ..."
    )


def test_python_m_app_ai_settings_help_still_runs(expected):
    env = {**os.environ, "COLUMNS": "80"}
    result = subprocess.run(
        [sys.executable, "-m", "app.ai.settings", "--help"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected["cli"][0]["out"]


def test_a_key_outside_every_area_rule_is_other():
    assert ai_settings.area_of("no.such.area") == ai_settings.OTHER_AREA
    with patch.object(ai_settings, "AREA_RULES", ()):
        assert ai_settings.area_of("scan.standard.enabled") == "Other"

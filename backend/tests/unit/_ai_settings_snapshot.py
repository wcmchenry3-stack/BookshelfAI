"""A behaviour snapshot of ``app.ai.settings`` (#807).

settings.py became a package. This builds a JSON-ready snapshot of what the
module did before the split: every setting definition (key, order,
description, bounds, default, and what its validator says about a fixed set
of probe values), every constant, and the CLI's exact output and exit code
for a set of commands. ``tests/fixtures/ai_settings_snapshot.json`` was
generated with this code from the single-module version on ``dev``;
``test_ai_settings_package.py`` checks the package still produces it.
The fixture was generated once, before the split; never regenerate it from
the package.

Everything env-dependent is pinned: the app settings the defaults read, the
registry spec ``get_feature`` returns, the terminal width argparse wraps at,
``$USER`` and the stored rows the CLI sees.
"""

import contextlib
import io
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.ai import settings as ai_settings

# The app settings the definitions' defaults read, pinned.
PINNED_SETTINGS = {
    "scan_enabled": True,
    "scan_enhanced_enabled": False,
    "scan_max_image_pixels": 40_000_000,
    "scan_global_daily_limit": 5_000,
    "scan_free_daily_limit": 3,
    "scan_premium_daily_limit": 7,
    "scan_free_no_match_daily_limit": 11,
    "scan_premium_no_match_daily_limit": 13,
    "scan_free_daily_ceiling_micros": 2_500_000,
    "ai_settings_cache_ttl_seconds": 30,
}

_REGISTRY = {
    "scan.standard": SimpleNamespace(provider="openai", model="gpt-4o-mini"),
    "scan.enhanced": SimpleNamespace(provider="openai", model="gpt-4o"),
}

# Values each validator is asked about; the answer (typed value or error
# message) pins the type and the bounds of every key.
PROBES = [
    True,
    False,
    None,
    -1,
    0,
    1,
    2,
    3,
    20,
    21,
    24,
    30,
    31,
    100,
    500,
    720,
    721,
    1_000,
    1_001,
    10_000,
    10_001,
    999_999,
    1_000_000,
    1_000_001,
    100_000_000,
    100_000_001,
    1_000_000_000,
    1_000_000_001,
    1.5,
    "",
    "true",
    "charge",
    "throttle",
    "openai",
    "anthropic",
    "gpt-4o",
    "gpt-4o-mini",
    "claude-haiku-5-5",
    "claude-sonnet-5-5",
    "x" * 101,
    [],
    {},
]


@contextlib.contextmanager
def pinned():
    """Pin everything the defaults and the CLI read from the environment."""
    with contextlib.ExitStack() as stack:
        for name, value in PINNED_SETTINGS.items():
            stack.enter_context(patch.object(ai_settings.settings, name, value))
        stack.enter_context(
            patch.object(ai_settings, "get_feature", lambda name: _REGISTRY[name])
        )
        yield


def _answer(validate, value):
    try:
        return {"ok": validate(value)}
    except ai_settings.AISettingValueError as exc:
        return {"error": str(exc)}


def definitions_snapshot() -> list[dict]:
    with pinned():
        return [
            {
                "key": key,
                "description": d.description,
                "bounds": d.bounds,
                "default": d.default(),
                "probes": [_answer(d.validate, p) for p in PROBES],
                "validate_value": [
                    _answer(lambda v, k=key: ai_settings.validate_value(k, v), p)
                    for p in PROBES[:8]
                ],
            }
            for key, d in ai_settings.DEFINITIONS.items()
        ]


def constants_snapshot() -> dict:
    """Every upper-case module constant, as JSON-ready data."""

    def plain(value):
        if isinstance(value, (frozenset, set)):
            return sorted(plain(v) for v in value)
        if isinstance(value, (tuple, list)):
            return [plain(v) for v in value]
        if hasattr(value, "items"):
            return {k: plain(v) for k, v in value.items()}
        return value

    return {
        name: plain(getattr(ai_settings, name))
        for name in sorted(dir(ai_settings))
        if name.isupper() and name != "DEFINITIONS"
    }


class _Row:
    def __init__(self, key, value, updated_by="seed"):
        self.key, self.value, self.updated_by = key, value, updated_by
        self.updated_at = datetime(2026, 10, 22, 9, 30, 5, tzinfo=UTC)


# The rows ``read_rows`` returns to the CLI: a valid row, an invalid row, an
# invalid switch, an operator's retired daily row (carried over), a retired
# monthly row and an unknown key.
def _stored() -> dict:
    rows = [
        _Row("scan.max_image_pixels", 25_000_000, "will"),
        _Row("scan.global_daily_limit", 0, "ops"),
        _Row("scan.enhanced.enabled", "false", "hand\x1b[31medit"),
        _Row("quota.premium.daily", 9, "ops"),
        _Row("quota.free.monthly", 50),
        _Row("scan.standard.model", "gpt-4o-mini"),
        _Row("mystery.key", 1),
    ]
    return {row.key: row for row in rows}


CLI_CASES = [
    ["--help"],
    ["set", "--help"],
    ["get", "--help"],
    ["list", "--help"],
    ["switch", "--help"],
    [],
    ["bogus"],
    ["get", "no.such.key"],
    ["list"],
    ["get", "scan.max_image_pixels"],
    ["get", "scan.global_daily_limit"],
    ["get", "scan.enhanced.enabled"],
    ["get", "quota.premium.found_daily"],
    ["get", "quota.free.found_daily"],
    ["get", "scan.standard.model"],
    ["set", "scan.max_image_pixels", "25000000", "--by", "will"],
    ["set", "scan.max_image_pixels", "999999"],
    ["set", "scan.max_image_pixels", "abc"],
    ["set", "scan.standard.enabled", "yes"],
    ["set", "scan.standard.enabled", "false"],
    ["set", "scan.enhanced.zero_result_action", "throttle", "--by", "  "],
    ["set", "scan.enhanced.zero_result_action", "nope"],
    ["set", "scan.standard.provider", "anthropic"],
    ["set", "scan.standard.model", "claude-haiku-5-5"],
    ["switch", "scan.standard", "anthropic"],
    ["switch", "scan.enhanced", "anthropic", "--model", "claude-haiku-5-5"],
    ["switch", "scan.enhanced", "anthropic", "--model", "gpt-4o"],
    ["switch", "scan.other", "anthropic"],
]


class _LogRecorder(logging.Handler):
    """Records every ``app.ai.settings`` log line (logger name, level and
    message) while the CLI runs, the same in and out of pytest."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines: list[list[str]] = []
        self._logger = logging.getLogger("app.ai.settings")

    def emit(self, record):
        if record.name.startswith("app.ai.settings"):
            self.lines.append([record.name, record.levelname, record.getMessage()])

    def __enter__(self):
        self._level = self._logger.level
        self._logger.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(self)
        return self

    def __exit__(self, *exc):
        logging.getLogger().removeHandler(self)
        self._logger.setLevel(self._level)


def _run_cli(argv: list[str], *, broken_db: bool = False) -> dict:
    db = AsyncMock()

    @asynccontextmanager
    async def _session():
        if broken_db:
            raise OSError("database down")
        yield db

    async def _read_rows(_db):
        return _stored()

    out, err = io.StringIO(), io.StringIO()
    logs = _LogRecorder()
    with (
        logs,
        patch.object(ai_settings.sentry_sdk, "capture_message") as capture,
        pinned(),
        patch.object(ai_settings, "_open_session", _session),
        patch.object(ai_settings, "read_rows", _read_rows),
        patch.object(ai_settings, "_dispose_engine", new=AsyncMock()),
        contextlib.redirect_stdout(out),
        contextlib.redirect_stderr(err),
    ):
        try:
            code = ai_settings.main(list(argv))
        except SystemExit as exc:
            code = exc.code
    return {
        "argv": argv,
        "code": code,
        "out": out.getvalue(),
        "err": err.getvalue(),
        "logs": logs.lines,
        "sentry": [[c.args, c.kwargs] for c in capture.call_args_list],
    }


def cli_snapshot() -> list[dict]:
    """Run every case. The caller pins ``COLUMNS`` and ``USER``."""
    results = [_run_cli(argv) for argv in CLI_CASES]
    results.append(_run_cli(["list"], broken_db=True))
    return results


def snapshot() -> dict:
    ai_settings.reset_cache()
    return {
        "definitions": definitions_snapshot(),
        "constants": constants_snapshot(),
        "keys_by_area": ai_settings.keys_by_area(),
        "cli": cli_snapshot(),
    }

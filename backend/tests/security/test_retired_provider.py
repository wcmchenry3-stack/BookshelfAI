"""The previous AI provider is gone for good (#538 part 2).

Anthropic is the only provider. This file holds the guards that keep it so:

- ``test_no_openai_anywhere_in_the_repo``: nothing in the repo may name the
  retired provider (a case-insensitive search for its name);
- a stored ``ai_settings`` row that still names it is ignored with a warning
  and the registry default (Anthropic) is used, so it can never crash a scan;
- a process env var that still names it (Render keeps one until the owner
  deletes it) must not stop the app from booting.

This file is excluded from the repo search itself (it has to spell the name).
"""

import logging
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from app.ai import settings as ai_settings
from app.ai.models_allowlist import ALLOWED_MODELS
from app.ai.registry import FEATURES
from app.core.config import Settings

pytestmark = pytest.mark.security

REPO = Path(__file__).resolve().parents[3]
THIS_FILE = Path(__file__).resolve()
NAME = "openai"

# Excluded from the search. Keep this list minimal; every entry needs a reason.
EXCLUDED_DIRS = {
    ".git",  # history, not content
    "node_modules",  # third-party packages
    "Pods",  # iOS third-party pods
    "__pycache__",  # bytecode
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    # Archived notes, if the folder exists: a historical record is not code.
    # Only this exact path is excluded: REPO / "docs" / "archive".
}
EXCLUDED_DOC_DIR = REPO / "docs" / "archive"
EXCLUDED_FILES = {
    # The privacy policy keeps one historical sentence saying that scans made
    # before 22 November 2026 were processed by the previous provider (users
    # are entitled to know). Owner/lawyer approved wording (#538 AC 8).
    REPO / "docs" / "privacy.html",
    # This test (it must spell the name).
    THIS_FILE,
}
# Lockfiles list transitive packages we do not control.
LOCKFILE_NAMES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Podfile.lock",
    "poetry.lock",
    "Pipfile.lock",
    "uv.lock",
}


def _tracked_files() -> list[Path]:
    """Files git knows about (tracked, plus untracked and not ignored), so
    ignored build output and local env files are never searched."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=REPO,
        check=True,
        capture_output=True,
    ).stdout
    return [REPO / name.decode() for name in out.split(b"\0") if name]


def _skipped(path: Path) -> bool:
    if path in EXCLUDED_FILES or path.name in LOCKFILE_NAMES:
        return True
    if EXCLUDED_DOC_DIR in path.parents:
        return True
    return bool(EXCLUDED_DIRS & set(path.relative_to(REPO).parts))


def _is_binary(data: bytes) -> bool:
    return b"\0" in data[:8192]


def find_mentions(files: list[Path]) -> list[str]:
    needle = NAME.lower().encode()
    hits = []
    for path in files:
        if _skipped(path) or not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if _is_binary(data) or needle not in data.lower():
            continue
        for n, line in enumerate(data.splitlines(), 1):
            if needle in line.lower():
                hits.append(f"{path.relative_to(REPO)}:{n}")
                break
    return hits


def test_no_openai_anywhere_in_the_repo():
    hits = find_mentions(_tracked_files())
    assert hits == [], (
        "the retired AI provider must not appear in the repo (#538); first "
        "match per file:\n" + "\n".join(hits)
    )


def test_the_search_finds_a_mention(tmp_path):
    """Guard against a vacuous search."""
    (tmp_path / "a.txt").write_text("uses OpenAI here\n")
    (tmp_path / "b.txt").write_text("clean\n")
    with patch(f"{__name__}.REPO", tmp_path), patch(
        f"{__name__}.EXCLUDED_FILES", set()
    ), patch(f"{__name__}.EXCLUDED_DOC_DIR", tmp_path / "docs" / "archive"):
        assert find_mentions([tmp_path / "a.txt", tmp_path / "b.txt"]) == ["a.txt:1"]


def test_the_search_skips_the_documented_exclusions(tmp_path):
    archive = tmp_path / "docs" / "archive"
    archive.mkdir(parents=True)
    (archive / "old.md").write_text("OPENAI\n")
    (tmp_path / "package-lock.json").write_text("openai\n")
    (tmp_path / "bin.dat").write_bytes(b"\0openai")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("openai\n")
    files = [
        archive / "old.md",
        tmp_path / "package-lock.json",
        tmp_path / "bin.dat",
        tmp_path / "node_modules" / "x.js",
    ]
    with patch(f"{__name__}.REPO", tmp_path), patch(
        f"{__name__}.EXCLUDED_FILES", set()
    ), patch(f"{__name__}.EXCLUDED_DOC_DIR", archive):
        assert find_mentions(files) == []


# ---------------------------------------------------------------------------
# A stored row that names the retired provider fails safe
# ---------------------------------------------------------------------------


def test_the_allow_list_has_one_provider():
    assert set(ALLOWED_MODELS) == {"anthropic"}
    assert {s.provider for s in FEATURES.values()} == {"anthropic"}


@pytest.mark.parametrize(
    "feature, retired_model",
    [("scan.standard", "gpt-4o-mini"), ("scan.enhanced", "gpt-4o")],
)
def test_a_stored_retired_provider_row_is_ignored_with_a_warning(
    feature, retired_model, caplog
):
    """Dev's table still held ``scan.enhanced.provider = openai`` and
    ``scan.enhanced.model = gpt-4o`` (the old seed) when this PR landed."""
    rows = {
        f"{feature}.provider": NAME,
        f"{feature}.model": retired_model,
    }
    with caplog.at_level(logging.WARNING, logger="app.ai.settings"):
        ai_settings.reset_cache(rows)
    try:
        stored = dict(ai_settings._cache.rows)
        spec = ai_settings.AISettings(stored).spec_for(FEATURES[feature])
    finally:
        ai_settings.reset_cache()
    assert stored == {}
    assert spec == FEATURES[feature]
    assert (spec.provider, spec.model) == ("anthropic", FEATURES[feature].model)
    assert f"ignoring {feature}.provider" in caplog.text
    assert f"ignoring {feature}.model" in caplog.text


def test_a_retired_model_with_the_anthropic_provider_falls_back():
    rows = {"scan.enhanced.provider": "anthropic", "scan.enhanced.model": "gpt-4o"}
    ai_settings.reset_cache(rows)
    try:
        stored = dict(ai_settings._cache.rows)
        spec = ai_settings.AISettings(stored).spec_for(FEATURES["scan.enhanced"])
    finally:
        ai_settings.reset_cache()
    assert stored == {"scan.enhanced.provider": "anthropic"}
    assert spec == FEATURES["scan.enhanced"]


def test_the_cli_refuses_to_switch_to_the_retired_provider():
    with pytest.raises(ai_settings.AISettingValueError):
        ai_settings.validate_switch("scan.standard", NAME)


@pytest.mark.parametrize("feature", ["scan.standard", "scan.enhanced"])
async def test_a_scan_with_a_stored_retired_row_still_runs_on_anthropic(feature):
    """Through the gateway, with the provider class faked at its boundary."""
    from unittest.mock import AsyncMock, MagicMock

    from app.ai.gateway import VisionPayload, run_feature
    from app.ai.providers.base import ProviderResult

    snapshot_rows = {f"{feature}.provider": NAME, f"{feature}.model": "gpt-4o"}
    ai_settings.reset_cache(snapshot_rows)
    try:
        snapshot = ai_settings.AISettings(dict(ai_settings._cache.rows))
        with patch("app.ai.gateway.AnthropicVisionProvider") as cls:
            cls.return_value.identify = AsyncMock(return_value=ProviderResult([]))
            result = await run_feature(
                feature,
                user=MagicMock(),
                db=MagicMock(),
                payload=VisionPayload(image_bytes=b"img"),
                settings_snapshot=snapshot,
            )
    finally:
        ai_settings.reset_cache()
    assert result.provider == "anthropic"
    assert result.model == FEATURES[feature].model
    cls.assert_called_once_with(FEATURES[feature])


# ---------------------------------------------------------------------------
# Leftover configuration must not stop boot
# ---------------------------------------------------------------------------

_DB = "postgresql+asyncpg://u:p@localhost/db"
_LEFTOVERS = {
    f"{NAME.upper()}_API_KEY": "sk-leftover-not-a-real-key",
    f"{NAME.upper()}_MAX_TOKENS": "512",
    "SCAN_MODEL": "gpt-4o-mini",
    "SCAN_ENHANCED_MODEL": "gpt-4o",
}


def test_leftover_process_env_vars_do_not_break_boot(monkeypatch):
    """The Render case: the dashboard still sets the old provider's key and
    the model names. pydantic-settings only reads process env vars that match
    a field, so the rest are ignored."""
    for name, value in _LEFTOVERS.items():
        monkeypatch.setenv(name, value)
    s = Settings(database_url=_DB, _env_file=None)
    for name in _LEFTOVERS:
        assert not hasattr(s, name.lower())
    assert "sk-leftover" not in repr(s)


def test_leftover_process_env_vars_do_not_break_a_fresh_interpreter():
    """The same, in a new process that imports the real module-level
    ``settings`` (boot) and builds the registry."""
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("SCAN_")},
        "DATABASE_URL": _DB,
        "TURNSTILE_REQUIRED": "false",
        **_LEFTOVERS,
    }
    code = (
        "from app.ai.registry import FEATURES;"
        "print(sorted((f.provider, f.model) for f in FEATURES.values()))"
    )
    done = subprocess.run(
        ["python", "-c", code],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-500:]
    assert "claude-haiku-5-5" in done.stdout and "claude-sonnet-5-5" in done.stdout
    assert "sk-leftover" not in done.stdout + done.stderr


def test_leftover_model_names_do_not_change_the_models(monkeypatch):
    """The registry is the only source of models: SCAN_MODEL is not read."""
    monkeypatch.setenv("SCAN_MODEL", "claude-sonnet-5-5")
    monkeypatch.setenv("SCAN_ENHANCED_MODEL", "claude-haiku-5-5")
    Settings(database_url=_DB, _env_file=None)
    assert FEATURES["scan.standard"].model == "claude-haiku-5-5"
    assert FEATURES["scan.enhanced"].model == "claude-sonnet-5-5"


def test_a_leftover_key_in_a_local_env_file_is_refused_by_name(tmp_path):
    """A key in a local ``.env`` that matches no field is refused (the same
    rule as every other retired setting, see docs/deployment.md "Upgrade
    notes"): delete the line. The error names the key, never its value."""
    from pydantic import ValidationError

    env_file = tmp_path / ".env"
    env_file.write_text(f"{NAME.upper()}_API_KEY=sk-leftover-not-a-real-key\n")
    with pytest.raises(ValidationError) as exc:
        Settings(database_url=_DB, _env_file=env_file)
    text = str(exc.value)
    assert f"{NAME}_api_key" in text
    assert "sk-leftover" not in text

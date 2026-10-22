"""Fixture tests for the Claude policy checker (#537).

Every check has a passing and a failing sample under
``tests/fixtures/claude_policy/``. Fixtures are ``.py.txt`` so linters and
pytest ignore them; the checker is fed their text under a pretend path.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "claude_policy"
SCRIPT = REPO / "scripts" / "policy" / "check_claude_policy.py"

_spec = importlib.util.spec_from_file_location("check_claude_policy", SCRIPT)
policy = importlib.util.module_from_spec(_spec)
sys.modules["check_claude_policy"] = policy
_spec.loader.exec_module(policy)

ALLOWED = frozenset({"claude-haiku-5-5", "claude-sonnet-5-5"})
PROVIDER = "backend/app/ai/providers/sample.py"
AI = "backend/app/ai/sample.py"


def run(name: str, path: str = PROVIDER):
    text = (FIXTURES / name).read_text()
    return policy.check_file(path, text, ALLOWED)


def checks(violations) -> set[str]:
    return {v.check for v in violations}


@pytest.mark.parametrize(
    "name",
    [
        "pass_provider.py.txt",
        "pass_dated_snapshot.py.txt",
        "pass_splat_request.py.txt",
        "pass_budget_503.py.txt",
    ],
)
def test_passing_fixtures(name):
    assert run(name) == []


@pytest.mark.security
def test_hardcoded_key_fails():
    assert "api-key" in checks(run("fail_hardcoded_key.py.txt"))


def test_retry_missing_529_fails():
    got = run("fail_retry_missing_529.py.txt")
    assert any(v.check == "retry" and "529" in v.message for v in got)


def test_retry_after_not_respected_fails():
    got = run("fail_retry_no_retry_after.py.txt")
    assert any(v.check == "retry" and "retry-after" in v.message for v in got)


def test_model_latest_alias_fails():
    got = run("fail_model_latest.py.txt")
    assert checks(got) == {"model-pin"}
    assert "moving alias" in got[0].message


def test_unlisted_model_fails():
    got = run("fail_model_unlisted.py.txt")
    assert checks(got) == {"model-pin"}
    assert "not in ALLOWED_MODELS" in got[0].message


def test_approved_ids_are_not_flagged_as_aliases():
    for model in ALLOWED:
        text = f'import anthropic\nM = "{model}"\n'
        assert policy.check_file(PROVIDER, text, ALLOWED) == []


def test_max_tokens_missing_fails():
    assert checks(run("fail_max_tokens_missing.py.txt")) == {"max-tokens"}


def test_stop_reason_missing_fails():
    assert checks(run("fail_stop_reason_missing.py.txt")) == {"stop-reason"}


def test_sdk_call_outside_providers_fails():
    assert "sdk-location" in checks(
        run("pass_provider.py.txt", "backend/app/api/scan.py")
    )


def test_raw_content_logging_fails_under_ai():
    assert checks(run("fail_log_raw_content.py.txt", AI)) == {"log-content"}


def test_raw_content_logging_ignored_outside_ai():
    assert run("fail_log_raw_content.py.txt", "backend/app/api/x.py") == []


def test_budget_refusal_must_be_503():
    assert checks(run("fail_budget_status.py.txt")) == {"budget-status"}


def test_detection_covers_app_ai_path_and_skips_env():
    assert policy.is_detected("backend/app/ai/x.py", "print(1)")
    assert policy.is_detected("a/b.py", "import anthropic")
    assert not policy.is_detected("backend/.env.production", "ANTHROPIC_API_KEY=x")
    assert not policy.is_detected("a/b.py", "print(1)")


def test_allowed_models_are_read_from_the_allowlist():
    assert policy.load_allowed_models(REPO) == ALLOWED


def test_real_repo_passes():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(REPO)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_cli_exits_nonzero_on_violation(tmp_path):
    (tmp_path / "backend/app/ai").mkdir(parents=True)
    allow = REPO / "backend/app/ai/models_allowlist.py"
    (tmp_path / "backend/app/ai/models_allowlist.py").write_text(allow.read_text())
    bad = tmp_path / "backend/app/ai/bad.py"
    bad.write_text('M = "claude-3-5-sonnet-latest"\n')
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path), "backend/app/ai/bad.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "model-pin" in result.stdout

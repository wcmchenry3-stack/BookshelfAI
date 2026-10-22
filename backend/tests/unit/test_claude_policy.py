"""Fixture tests for the Claude policy checker (#537).

Every check has a passing and a failing sample under
``tests/fixtures/claude_policy/``. Fixtures are ``.py.txt`` so linters and
pytest ignore them; the checker is fed their text under a pretend path.
"""

import importlib.util
import shutil
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


APP = "backend/app/api/sample.py"


def test_budget_refusal_must_be_503():
    assert checks(run("fail_budget_status.py.txt", APP)) == {"budget-status"}
    assert checks(run("fail_budget_status_429.py.txt", APP)) == {"budget-status"}


def test_budget_503_passes_in_any_app_file():
    assert run("pass_budget_503.py.txt", APP) == []


def test_real_scan_router_budget_refusal_passes():
    rel = "backend/app/api/scan.py"
    assert policy.check_file(rel, (REPO / rel).read_text(), ALLOWED) == []


def test_comments_and_docstrings_do_not_satisfy_retry_or_refusal():
    got = run("fail_retry_comments_only.py.txt")
    assert {"retry", "stop-reason"} <= checks(got)
    assert any("429" in v.message for v in got)
    assert any("retry-after header is not respected" in v.message for v in got)


REAL_PROVIDER = REPO / "backend/app/ai/providers/anthropic_vision.py"


def test_real_provider_copy_passes():
    assert policy.check_file(PROVIDER, REAL_PROVIDER.read_text(), ALLOWED) == []


def test_real_provider_without_529_fails():
    text = REAL_PROVIDER.read_text()
    assert "{429, 500, 503, 529}" in text
    mutated = text.replace("{429, 500, 503, 529}", "{429, 500, 503}")
    got = policy.check_file(PROVIDER, mutated, ALLOWED)
    assert any(v.check == "retry" and "529" in v.message for v in got)


def test_real_provider_without_retry_after_fails():
    mutated = (
        REAL_PROVIDER.read_text()
        .replace("retry_after", "ra")
        .replace("retry-after", "x-ra")
    )
    got = policy.check_file(PROVIDER, mutated, ALLOWED)
    assert any(v.check == "retry" and "retry-after" in v.message for v in got)


def test_real_scan_router_with_402_budget_fails():
    rel = "backend/app/api/scan.py"
    text = (REPO / rel).read_text()
    old = 'status_code=status.HTTP_503_SERVICE_UNAVAILABLE,\n            detail="ai_budget_reached"'
    assert old in text
    mutated = text.replace(
        old, 'status_code=402,\n            detail="ai_budget_reached"'
    )
    assert "budget-status" in checks(policy.check_file(rel, mutated, ALLOWED))


def test_stream_without_max_tokens_fails():
    assert checks(run("fail_stream_no_max_tokens.py.txt")) == {"max-tokens"}


def test_with_raw_response_create_without_max_tokens_fails():
    assert checks(run("fail_raw_response_no_max_tokens.py.txt")) == {"max-tokens"}


def test_raw_response_and_batches_are_sdk_calls_outside_providers():
    for call in (
        "client.messages.with_raw_response.create()",
        "client.messages.batches.create()",
        "client.beta.messages.create()",
    ):
        got = policy.check_file("backend/app/api/x.py", f"{call}\n", ALLOWED)
        assert "sdk-location" in checks(got), call


def test_syntax_error_fails_closed():
    assert checks(run("fail_syntax_error.py.txt")) == {"parse"}


@pytest.mark.parametrize(
    "text", ["KEY = os.environ['ANTHROPIC_API_KEY']\n", "// @anthropic-ai/sdk\n"]
)
def test_detection_by_content(text):
    assert policy.is_detected("a/b.py", text)
    assert "model-pin" in checks(
        policy.check_file(
            "a/b.ts", text + 'const m = "claude-3-5-sonnet-latest"\n', ALLOWED
        )
    )


def test_injected_client_call_is_detected():
    assert policy.is_detected("backend/app/api/x.py", "self.client.messages.create(x)")


def test_log_harmless_attributes_pass():
    assert run("pass_log_harmless_attrs.py.txt", AI) == []


@pytest.mark.parametrize(
    "name,count",
    [("fail_log_method_calls.py.txt", 5), ("fail_log_unsafe_attr_read.py.txt", 1)],
)
def test_log_method_calls_and_unsafe_attr_reads_fail(name, count):
    got = run(name, AI)
    assert checks(got) == {"log-content"} and len(got) == count


def test_log_nested_call_argument_fails():
    assert checks(run("fail_log_nested_call_arg.py.txt", AI)) == {"log-content"}


def test_real_provider_log_wrapper_replaced_fails():
    text = REAL_PROVIDER.read_text()
    assert text.count("        len(text),\n") == 1
    mutated = text.replace("        len(text),\n", "        text.strip(),\n")
    got = policy.check_file(PROVIDER, mutated, ALLOWED)
    assert "log-content" in checks(got)
    assert "log-content" not in checks(policy.check_file(PROVIDER, text, ALLOWED))


def test_real_provider_request_without_max_tokens_fails():
    text = REAL_PROVIDER.read_text()
    old = '        "max_tokens": spec.max_tokens,\n'
    assert text.count(old) == 1
    got = policy.check_file(PROVIDER, text.replace(old, ""), ALLOWED)
    assert "max-tokens" in checks(got)


@pytest.mark.parametrize(
    "name", ["fail_splat_without_max_tokens.py.txt", "fail_splat_unresolvable.py.txt"]
)
def test_splat_without_resolvable_max_tokens_fails(name):
    assert checks(run(name)) == {"max-tokens"}


def test_streaming_response_without_max_tokens_fails():
    assert checks(run("fail_streaming_response_no_max_tokens.py.txt")) == {"max-tokens"}


def test_positional_budget_status_fails():
    assert checks(run("fail_budget_positional.py.txt", APP)) == {"budget-status"}


def test_budget_httpstatus_passes():
    assert run("pass_budget_httpstatus.py.txt", APP) == []


def test_one_splat_with_max_tokens_is_enough():
    """client.messages.create(**request, **{"extra_headers": {}}) passes when
    request supplies max_tokens; it fails when no mapping does."""
    src = (
        "import anthropic\n"
        "def build():\n"
        '    return {"model": "claude-haiku-5-5", "max_tokens": 10}\n'
        "async def call(client):\n"
        "    request = build()\n"
        '    r = await client.messages.create(**request, **{"extra_headers": {}})\n'
        "    return r.stop_reason\n"
    )
    rel = "backend/app/ai/providers/sample.py"
    assert "max-tokens" not in checks(policy.check_file(rel, src, ALLOWED))
    bad = src.replace(', "max_tokens": 10', "")
    assert "max-tokens" in checks(policy.check_file(rel, bad, ALLOWED))


def test_budget_mixed_positional_status_and_keyword_detail():
    """HTTPException(503, detail=...) is compliant; (402, detail=...) is not."""
    ok = 'from fastapi import HTTPException\nraise HTTPException(503, detail="ai_budget_reached")\n'
    bad = ok.replace("503", "402")
    assert policy.check_file(APP, ok, ALLOWED) == []
    assert checks(policy.check_file(APP, bad, ALLOWED)) == {"budget-status"}


def test_log_content_attribute_fails():
    assert checks(run("fail_log_attr_content.py.txt", AI)) == {"log-content"}


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


def _allowlist_root(tmp_path, source: str) -> Path:
    d = tmp_path / "backend/app/ai"
    d.mkdir(parents=True)
    (d / "models_allowlist.py").write_text(source)
    return tmp_path


def _run_cli(root: Path):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_allowlist_without_anthropic_key_exits_nonzero(tmp_path):
    root = _allowlist_root(
        tmp_path, (FIXTURES / "fail_allowlist_no_anthropic.py.txt").read_text()
    )
    result = _run_cli(root)
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert "anthropic" in result.stderr


@pytest.mark.parametrize(
    "source",
    ["x = (\n", "OTHER = {}\n", "ALLOWED_MODELS = {'anthropic': 'claude-opus-9-9'}\n"],
)
def test_allowlist_unreadable_fails_closed(tmp_path, source):
    result = _run_cli(_allowlist_root(tmp_path, source))
    assert result.returncode != 0
    assert "Traceback" not in result.stderr


def test_missing_allowlist_fails_closed(tmp_path):
    result = _run_cli(tmp_path)
    assert result.returncode != 0
    assert "Traceback" not in result.stderr


def test_decoy_dict_does_not_widen_the_allowlist(tmp_path):
    root = _allowlist_root(tmp_path, (FIXTURES / "decoy_allowlist.py.txt").read_text())
    allowed = policy.load_allowed_models(root)
    assert allowed == frozenset({"claude-haiku-5-5"})
    bad = policy.check_file(
        PROVIDER, 'import anthropic\nM = "claude-opus-9-9"\n', allowed
    )
    ok = policy.check_file(
        PROVIDER, 'import anthropic\nM = "claude-haiku-5-5"\n', allowed
    )
    assert checks(bad) == {"model-pin"} and ok == []


def test_fallback_walk_skips_virtualenvs(tmp_path, monkeypatch):
    for d in (".venv", "venv", "site-packages", "node_modules", "src"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "m.py").write_text("x = 1\n")
    monkeypatch.setattr(
        policy.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError())
    )
    assert policy._tracked_files(tmp_path) == ["src/m.py"]


@pytest.mark.skipif(shutil.which("jq") is None, reason="policy-gate.sh needs jq")
class TestPolicyGateHook:
    """Run the real hook in a throwaway git repo."""

    def _repo(self, tmp_path, rel: str, content: str) -> Path:
        git = [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
        ]
        (tmp_path / ".claude/hooks").mkdir(parents=True)
        for f in (
            ".claude/hooks/policy-gate.sh",
            ".claude/policies/policy-patterns.json",
        ):
            (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REPO / f, tmp_path / f)
        subprocess.run(
            [*git, "-c", "init.defaultBranch=main", "init", "-q"],
            cwd=tmp_path,
            check=True,
        )
        (tmp_path / "README.txt").write_text("base\n")
        subprocess.run([*git, "add", "-A"], cwd=tmp_path, check=True)
        subprocess.run([*git, "commit", "-qm", "base"], cwd=tmp_path, check=True)
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        subprocess.run([*git, "add", "-A"], cwd=tmp_path, check=True)
        subprocess.run([*git, "commit", "-qm", "change"], cwd=tmp_path, check=True)
        return tmp_path

    def _gate(self, repo: Path):
        payload = '{"tool_input": {"command": "gh pr create"}}'
        return subprocess.run(
            ["bash", ".claude/hooks/policy-gate.sh"],
            cwd=repo,
            input=payload,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_path_only_match_under_app_ai_blocks(self, tmp_path):
        repo = self._repo(tmp_path, "backend/app/ai/registry.py", "X = 1\n")
        result = self._gate(repo)
        assert result.returncode == 2
        assert "claude" in result.stderr

    def test_injected_sdk_call_content_blocks(self, tmp_path):
        repo = self._repo(
            tmp_path, "backend/app/other/x.py", "self.c.messages.create(x)\n"
        )
        assert self._gate(repo).returncode == 2

    def test_unrelated_path_and_content_passes(self, tmp_path):
        repo = self._repo(tmp_path, "backend/app/other/x.py", "X = 1\n")
        assert self._gate(repo).returncode == 0

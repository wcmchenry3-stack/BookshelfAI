"""AI choke point for the scan evaluation harness (#536 review).

``backend/eval/`` lives outside ``backend/app`` so the existing choke-point
test (``test_ai_choke_point.py``, unchanged) does not scan it. The harness
may reach a provider only through ``app.ai.gateway``: no file under
``backend/eval/`` may import a provider SDK, ``app.ai.providers``, name a
provider API host or read a provider API key. Reuses the existing checker.
"""

import ast
from pathlib import Path

import pytest

from tests.security.test_ai_choke_point import find_provider_calls

pytestmark = pytest.mark.security

EVAL_ROOT = Path(__file__).resolve().parents[2] / "eval"


def _python_files() -> list[Path]:
    return sorted(EVAL_ROOT.rglob("*.py"))


def test_eval_harness_exists():
    # Guard against a vacuous pass (a moved or renamed harness).
    names = {p.name for p in _python_files()}
    assert {"runner.py", "run_eval.py", "dataset.py"} <= names


def test_no_provider_access_under_eval():
    # No exemption: allowed= names a directory that does not exist.
    violations = find_provider_calls(EVAL_ROOT, allowed=Path("does-not-exist"))
    assert violations == [], "\n".join(str(v) for v in violations)


def test_the_runner_reaches_providers_through_the_gateway():
    tree = ast.parse((EVAL_ROOT / "ai_scan" / "runner.py").read_text())
    imported = {
        alias.name if node.module is None else f"{node.module}.{alias.name}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "app.ai.gateway" in imported


@pytest.mark.parametrize(
    "leak",
    [
        "import anthropic\n",
        "from openai import AsyncOpenAI\n",
        "from app.ai.providers.anthropic_vision import AnthropicVisionProvider\n",
        "import app.ai.providers.openai_vision\n",
    ],
)
def test_checker_catches_a_leak_in_an_eval_like_tree(tmp_path, leak):
    (tmp_path / "ai_scan").mkdir()
    (tmp_path / "ai_scan" / "runner.py").write_text(leak)
    assert find_provider_calls(tmp_path, allowed=Path("does-not-exist"))

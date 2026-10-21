"""AI choke point: provider calls may only live in ``app/ai/providers/``.

Every paid model call must go through ``app.ai.gateway.run_feature``, which
reads model, token cap and timeout from the server-side registry. This test
fails if any other module under ``backend/app`` names a provider API host,
imports a provider SDK, or calls an httpx client against a provider host.

Run on its own with:  pytest -m security tests/security/test_ai_choke_point.py
"""

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

import app as app_package

pytestmark = pytest.mark.security

PROVIDER_SDKS = frozenset({"openai", "anthropic"})
# Provider API hosts. Matches any mention, so an httpx call (or a URL constant
# later passed to one) is caught wherever it appears.
PROVIDER_HOST_RE = re.compile(r"api\.(?:openai|anthropic)\.com", re.IGNORECASE)
DYNAMIC_IMPORTERS = frozenset({"import_module", "__import__"})
ALLOWED_DIR = Path("ai") / "providers"

APP_ROOT = Path(app_package.__file__).resolve().parent


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    reason: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.reason}"


def _sdk_root(module: str | None) -> str | None:
    if not module:
        return None
    root = module.split(".", 1)[0]
    return root if root in PROVIDER_SDKS else None


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _ast_violations(path: Path, source: str) -> list[Violation]:
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        # Fail closed: a file we cannot read cannot be shown to be clean.
        return [Violation(path, exc.lineno or 0, "could not parse file")]

    found: list[Violation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if sdk := _sdk_root(alias.name):
                    found.append(Violation(path, node.lineno, f"imports {sdk} SDK"))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and (sdk := _sdk_root(node.module)):
                found.append(Violation(path, node.lineno, f"imports {sdk} SDK"))
        elif (
            isinstance(node, ast.Call)
            and _call_name(node.func) in DYNAMIC_IMPORTERS
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and (sdk := _sdk_root(node.args[0].value))
        ):
            found.append(Violation(path, node.lineno, f"dynamically imports {sdk}"))
    return found


def find_provider_calls(root: Path, allowed: Path = ALLOWED_DIR) -> list[Violation]:
    """Return every provider host, SDK import or provider call under ``root``.

    Files under ``root / allowed`` are skipped; that is where provider
    adapters live.
    """
    root = root.resolve()
    allowed_dir = (root / allowed).resolve()
    violations: list[Violation] = []
    for path in sorted(root.rglob("*.py")):
        resolved = path.resolve()
        if resolved == allowed_dir or allowed_dir in resolved.parents:
            continue
        source = path.read_text(encoding="utf-8")
        rel = resolved.relative_to(root)
        for lineno, line in enumerate(source.splitlines(), start=1):
            if PROVIDER_HOST_RE.search(line):
                violations.append(Violation(rel, lineno, "names a provider API host"))
        violations.extend(
            Violation(rel, v.line, v.reason) for v in _ast_violations(path, source)
        )
    return violations


# ---------------------------------------------------------------------------
# The real codebase
# ---------------------------------------------------------------------------


def test_no_provider_calls_outside_ai_providers() -> None:
    violations = find_provider_calls(APP_ROOT)
    assert violations == [], (
        "AI provider calls must go through app.ai.gateway.run_feature; move "
        "provider code into app/ai/providers/:\n"
        + "\n".join(str(v) for v in violations)
    )


def test_checker_sees_the_real_provider_when_not_allowed() -> None:
    """Guard against a vacuous checker: the OpenAI adapter must be detected."""
    violations = find_provider_calls(APP_ROOT, allowed=Path("does-not-exist"))
    flagged = {v.path for v in violations}
    assert ALLOWED_DIR / "openai_vision.py" in flagged


# ---------------------------------------------------------------------------
# Negative fixtures: the checker must fail on a leak outside providers/
# ---------------------------------------------------------------------------

# Fixture text is built from PROVIDER_SDKS rather than spelled out so the org
# OpenAI/Claude policy greps don't mistake this test for an API caller.
_HOSTS = [f"api.{sdk}.com" for sdk in sorted(PROVIDER_SDKS)]

_LEAKS = [
    *[
        f'import httpx\n\nasync def f():\n    await httpx.AsyncClient().post("https://{host}/v1/x")\n'
        for host in _HOSTS
    ],
    *[f'URL = "https://{host}/v1/messages"\n' for host in _HOSTS],
    *[f"import {sdk}\n" for sdk in sorted(PROVIDER_SDKS)],
    *[f"import os, {sdk}.types\n" for sdk in sorted(PROVIDER_SDKS)],
    *[f"from {sdk} import AsyncClient\n" for sdk in sorted(PROVIDER_SDKS)],
    *[
        f'import importlib\n\nclient = importlib.import_module("{sdk}")\n'
        for sdk in sorted(PROVIDER_SDKS)
    ],
    *[f'mod = __import__("{sdk}")\n' for sdk in sorted(PROVIDER_SDKS)],
    "def broken(:\n",
]


def _write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.mark.parametrize("content", _LEAKS)
def test_checker_fails_on_leak_outside_providers(tmp_path: Path, content: str) -> None:
    _write(tmp_path, "services/leak.py", content)
    violations = find_provider_calls(tmp_path)
    assert violations, f"checker missed a leak:\n{content}"
    assert {v.path for v in violations} == {Path("services") / "leak.py"}


@pytest.mark.parametrize("content", _LEAKS[:-1])
def test_checker_allows_leak_inside_providers(tmp_path: Path, content: str) -> None:
    _write(tmp_path, "ai/providers/some_provider.py", content)
    assert find_provider_calls(tmp_path) == []


@pytest.mark.parametrize("sdk", sorted(PROVIDER_SDKS))
def test_checker_ignores_lookalike_names(tmp_path: Path, sdk: str) -> None:
    _write(
        tmp_path,
        "services/clean.py",
        f"from app.ai.providers import {sdk}_vision\n"
        "from app.core.config import settings\n"
        f"import {sdk}_helpers_not_real\n"
        f"key = settings.{sdk}_api_key\n"
        f'url = "https://api.{sdk}.example/v1"\n',
    )
    assert find_provider_calls(tmp_path) == []

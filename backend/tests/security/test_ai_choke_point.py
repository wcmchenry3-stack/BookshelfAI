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
# Wrappers and other vendors' SDKs. Matched on the full dotted module path
# (``google.genai`` or ``google.genai.x``), so ``google.cloud.storage`` and a
# bare ``import google`` are not flagged.
OTHER_SDK_MODULES = frozenset(
    {
        "litellm",
        "langchain_openai",
        "langchain_anthropic",
        "google.genai",
        "google.generativeai",
        "mistralai",
        "cohere",
    }
)
_ALL_SDK_MODULES = PROVIDER_SDKS | OTHER_SDK_MODULES
# Provider API key names: settings attributes (any case) and environment keys.
API_KEY_ATTR_RE = re.compile(
    r"^(openai|anthropic|gemini|google|mistral|cohere)_api_key$", re.IGNORECASE
)
API_KEY_ENV_RE = re.compile(
    r"^(OPENAI|ANTHROPIC|GEMINI|GOOGLE|MISTRAL|COHERE)_API_KEY$"
)
# Only the gateway may import the provider adapters. Modules inside
# app/ai/providers/ are skipped entirely. Other app/ai/ modules (registry,
# models_allowlist, __init__) must not import them.
GATEWAY_FILE = Path("ai") / "gateway.py"
# Narrow, explicit exceptions to the API-key rule: (path under app/, reason).
# Currently empty: the only reference outside providers is the field
# definition in core/config.py, which is an AnnAssign target, not an access.
API_KEY_ALLOWLIST: dict[Path, str] = {}
# Provider API hosts. Matches any mention, so an httpx call (or a URL constant
# later passed to one) is caught wherever it appears.
PROVIDER_HOST_RE = re.compile(r"api\.(?:openai|anthropic)\.com", re.IGNORECASE)
DYNAMIC_IMPORTERS = frozenset({"import_module", "__import__"})
ALLOWED_DIR = Path("ai") / "providers"
PROVIDERS_MODULE = "app.ai.providers"

APP_ROOT = Path(app_package.__file__).resolve().parent


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    reason: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.reason}"


def _sdk_root(module: str | None) -> str | None:
    """Return the matched SDK name if ``module`` is or is under one."""
    if not module:
        return None
    parts = module.split(".")
    for i in range(1, len(parts) + 1):
        prefix = ".".join(parts[:i])
        if prefix in _ALL_SDK_MODULES:
            return prefix
    return None


def _is_os_environ(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _const_str(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _api_key_reason(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute) and API_KEY_ATTR_RE.match(node.attr):
        return f"reads provider API key ({node.attr})"
    key: str | None = None
    if isinstance(node, ast.Subscript) and _is_os_environ(node.value):
        key = _const_str(node.slice)
    elif isinstance(node, ast.Call) and node.args:
        func = node.func
        if isinstance(func, ast.Attribute) and (
            (func.attr == "get" and _is_os_environ(func.value))
            or (
                func.attr == "getenv"
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
            )
        ):
            key = _const_str(node.args[0])
    if key and API_KEY_ENV_RE.match(key):
        return f"reads provider API key from environment ({key})"
    return None


def _imports_provider_package(module: str | None) -> bool:
    """True for ``app.ai.providers`` and anything beneath it."""
    return bool(module) and (
        module == PROVIDERS_MODULE or module.startswith(PROVIDERS_MODULE + ".")
    )


def _resolve_relative(path: Path, level: int, module: str | None) -> str | None:
    """Resolve a relative import in ``path`` to a dotted name under ``app``.

    ``path`` is relative to the app root (``services/leak.py``). Returns None if
    the import climbs out of the package.
    """
    package = list(path.with_suffix("").parts[:-1])
    if path.name == "__init__.py":
        package = list(path.parts[:-1])
    drop = level - 1
    if drop > len(package):
        return None
    base = ["app", *package[: len(package) - drop]]
    return ".".join([*base, *([module] if module else [])])


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _ast_violations(
    path: Path, source: str, rel: Path | None = None
) -> list[Violation]:
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        # Fail closed: a file we cannot read cannot be shown to be clean.
        return [Violation(path, exc.lineno or 0, "could not parse file")]

    # Only the gateway may import the providers.
    in_ai_package = rel == GATEWAY_FILE
    key_allowed = rel in API_KEY_ALLOWLIST
    found: list[Violation] = []
    for node in ast.walk(tree):
        if not key_allowed and (reason := _api_key_reason(node)):
            found.append(Violation(path, getattr(node, "lineno", 0), reason))
        if isinstance(node, ast.Import):
            for alias in node.names:
                if sdk := _sdk_root(alias.name):
                    found.append(Violation(path, node.lineno, f"imports {sdk} SDK"))
                if not in_ai_package and _imports_provider_package(alias.name):
                    found.append(
                        Violation(path, node.lineno, "imports app.ai.providers")
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and (sdk := _sdk_root(node.module)):
                found.append(Violation(path, node.lineno, f"imports {sdk} SDK"))
            module = node.module
            if node.level:
                module = _resolve_relative(rel or path, node.level, node.module)
            if not in_ai_package and (
                _imports_provider_package(module)
                or (
                    module == "app.ai"
                    and any(a.name == "providers" for a in node.names)
                )
            ):
                found.append(Violation(path, node.lineno, "imports app.ai.providers"))
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
            Violation(rel, v.line, v.reason) for v in _ast_violations(path, source, rel)
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

_NEW_LEAKS = [
    "from app.core.config import settings\n\nkey = settings.openai_api_key\n",
    "from app.core.config import settings\n\nkey = settings.ANTHROPIC_API_KEY\n",
    'import os\n\nkey = os.getenv("ANTHROPIC_API_KEY")\n',
    'import os\n\nkey = os.environ.get("OPENAI_API_KEY")\n',
    'import os\n\nkey = os.environ["GEMINI_API_KEY"]\n',
    "import litellm\n",
    "from litellm import completion\n",
    "from google.genai import Client\n",
    "import google.generativeai as genai\n",
    "from google.genai.types import Part\n",
    "from langchain_openai import ChatOpenAI\n",
    "from langchain_anthropic import ChatAnthropic\n",
    "import mistralai\n",
    "import cohere\n",
    'import importlib\n\nm = importlib.import_module("litellm")\n',
]

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
    "from app.ai.providers.openai_vision import OpenAIVisionProvider\n",
    "import app.ai.providers.openai_vision\n",
    "from app.ai import providers\n",
    "from ..ai.providers import openai_vision\n",
    "from ..ai.providers.openai_vision import OpenAIVisionProvider\n",
    *_NEW_LEAKS,
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
        "from app.ai.gateway import run_feature\n"
        f"from app.ai.providers_extra import {sdk}_vision\n"
        "from app.core.config import settings\n"
        f"import {sdk}_helpers_not_real\n"
        f"configured = settings.{sdk}_api_key_configured\n"
        f'url = "https://api.{sdk}.example/v1"\n',
    )
    assert find_provider_calls(tmp_path) == []


def test_gateway_inside_ai_package_may_import_providers(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "ai/gateway.py",
        "from app.ai.providers.openai_vision import OpenAIVisionProvider\n",
    )
    assert find_provider_calls(tmp_path) == []


def test_other_ai_module_may_not_import_providers(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "ai/batch_scan.py",
        "from app.ai.providers.openai_vision import OpenAIVisionProvider\n",
    )
    violations = find_provider_calls(tmp_path)
    assert {v.path for v in violations} == {Path("ai") / "batch_scan.py"}


@pytest.mark.parametrize("name", ["registry.py", "__init__.py", "models_allowlist.py"])
def test_ai_support_modules_may_not_import_providers(tmp_path: Path, name: str) -> None:
    _write(tmp_path, f"ai/{name}", "from .providers import base\n")
    assert find_provider_calls(tmp_path), name


def test_checker_ignores_lookalike_sdk_and_key_names(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "services/clean.py",
        "import google\n"
        "import google.cloud.storage\n"
        "from google.cloud import storage\n"
        "import litellm_helpers_not_real\n"
        "import cohere_extras\n"
        "import os\n"
        "from app.core.config import settings\n"
        "a = settings.openai_api_key_configured\n"
        "b = settings.my_openai_api_key\n"
        'c = os.getenv("OPENAI_API_KEY_CONFIGURED")\n'
        'd = os.getenv("SOME_OTHER_KEY")\n'
        'e = os.environ.get("PATH")\n',
    )
    assert find_provider_calls(tmp_path) == []


def test_config_field_definition_is_not_an_attribute_access(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "core/config.py",
        'class Settings:\n    openai_api_key: str = ""\n',
    )
    assert find_provider_calls(tmp_path) == []

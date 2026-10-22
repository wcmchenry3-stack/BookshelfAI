#!/usr/bin/env python3
"""Claude / Anthropic API policy check (#537). Stdlib only.

Mirrors ``.claude/policies/claude.md``. Scans tracked source files that touch
the Anthropic SDK or live under ``app/ai/`` and exits non-zero on any
violation. Run from anywhere: ``python3 scripts/policy/check_claude_policy.py``
(optionally ``--root DIR`` and explicit file paths relative to the root).

``check_file(relpath, text, allowed_models)`` is the pure entry point the tests
use; it never touches the disk.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DETECT = re.compile(
    r"from anthropic|import anthropic|@anthropic-ai/sdk|Anthropic\(|"
    r"ANTHROPIC_API_KEY|claude_agent_sdk"
)
AI_DIR = re.compile(r"(^|/)app/ai/")
PROVIDERS_DIR = re.compile(r"(^|/)app/ai/providers/")
ENV_FILE = re.compile(r"(^|/)\.env")

SCAN_EXT = (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs")
# Prose, fixtures and the checker itself are not production code.
EXCLUDE = re.compile(
    r"(^|/)(node_modules|tests|__tests__|docs|\.claude|\.github|scripts/policy)/"
    r"|\.(test|spec)\.[jt]sx?$"
)

KEY = re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")
MODEL_ID = re.compile(r"""["'](claude-[A-Za-z0-9._-]+)["']""")
DATED = re.compile(r"-\d{8}$")
SDK_TEXT = re.compile(r"\.messages\.(create|stream)\(|\bAsyncAnthropic\(|\bAnthropic\(")
BUDGET = re.compile(r"\b(\d{3})\b[\s`'\"]*ai_budget_reached")
RAW_FIELDS = frozenset(
    {
        "text",
        "content",
        "raw",
        "raw_output",
        "output",
        "completion",
        "response_text",
        "reply",
        "image",
        "image_bytes",
        "b64",
    }
)
SAFE_WRAPPERS = frozenset({"len", "sha256", "hexdigest", "md5", "sha1"})
LOG_NAMES = frozenset({"logger", "log", "logging"})
LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)


@dataclass(frozen=True)
class Violation:
    path: str
    check: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: [{self.check}] {self.message}"


def load_allowed_models(root: Path) -> frozenset[str]:
    """The approved Claude IDs: ``ALLOWED_MODELS["anthropic"]`` in
    ``backend/app/ai/models_allowlist.py``, read without importing the app."""
    src = (root / "backend/app/ai/models_allowlist.py").read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == "anthropic":
                    return frozenset(
                        c.value
                        for c in ast.walk(v)
                        if isinstance(c, ast.Constant) and isinstance(c.value, str)
                    )
    raise SystemExit("models_allowlist.py has no ALLOWED_MODELS['anthropic']")


def is_detected(relpath: str, text: str) -> bool:
    if ENV_FILE.search(relpath):
        return False
    return bool(DETECT.search(text) or AI_DIR.search(relpath))


def _call_name(func: ast.expr) -> str:
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _chain(func: ast.expr) -> list[str]:
    parts: list[str] = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        parts.append(func.id)
    return parts[::-1]


def _is_sdk_call(node: ast.Call) -> str | None:
    chain = _chain(node.func)
    if (
        len(chain) >= 2
        and chain[-2] == "messages"
        and chain[-1] in ("create", "stream")
    ):
        return "messages"
    if chain and chain[-1] in ("AsyncAnthropic", "Anthropic"):
        return "client"
    return None


def _py_sdk_calls(tree: ast.AST) -> list[tuple[ast.Call, str]]:
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            kind = _is_sdk_call(n)
            if kind:
                out.append((n, kind))
    return out


def _raw_refs(node: ast.AST) -> list[str]:
    """Raw content names referenced by a logging argument, ignoring anything
    wrapped in len()/sha256()/hexdigest()."""
    found: list[str] = []

    def visit(n: ast.AST) -> None:
        if isinstance(n, ast.Call) and _call_name(n.func) in SAFE_WRAPPERS:
            return
        if isinstance(n, ast.Name) and n.id in RAW_FIELDS:
            found.append(n.id)
        elif isinstance(n, ast.Attribute) and n.attr in RAW_FIELDS:
            found.append(n.attr)
        elif (
            isinstance(n, ast.Subscript)
            and isinstance(n.slice, ast.Constant)
            and n.slice.value in RAW_FIELDS
        ):
            found.append(str(n.slice.value))
        for child in ast.iter_child_nodes(n):
            visit(child)

    visit(node)
    return found


def check_file(
    relpath: str, text: str, allowed_models: frozenset[str]
) -> list[Violation]:
    if not is_detected(relpath, text):
        return []
    v: list[Violation] = []

    def fail(check: str, msg: str) -> None:
        v.append(Violation(relpath, check, msg))

    is_py = relpath.endswith(".py")
    tree: ast.AST | None = None
    if is_py:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            fail("parse", "file does not parse; the policy cannot be verified")
            return v

    # 1. No hardcoded key.
    if KEY.search(text):
        fail(
            "api-key", "Hardcoded Anthropic API key (sk-ant-...). Use the environment."
        )

    # 3. Pinned model IDs.
    for model in sorted(set(MODEL_ID.findall(text))):
        if model.endswith("-latest"):
            fail("model-pin", f"{model!r} is a moving alias; use an approved fixed ID.")
        elif model not in allowed_models and not DATED.search(model):
            fail(
                "model-pin",
                f"{model!r} is not in ALLOWED_MODELS['anthropic'] and is not a dated snapshot.",
            )

    # 8. Budget breaker refusal is 503.
    for m in BUDGET.finditer(text):
        if m.group(1) != "503":
            fail("budget-status", f"ai_budget_reached must be 503, found {m.group(1)}.")

    # SDK calls.
    if tree is not None:
        calls = _py_sdk_calls(tree)
    else:
        calls = [
            (None, "messages" if m.group(1) else "client")
            for m in SDK_TEXT.finditer(text)
        ]
    has_sdk = bool(calls)

    # 6. SDK only under app/ai/providers/.
    if has_sdk and not PROVIDERS_DIR.search(relpath):
        fail(
            "sdk-location",
            "Anthropic SDK calls are only allowed under backend/app/ai/providers/.",
        )

    if has_sdk:
        # 2. Retry on 429/500/503/529 with retry-after.
        missing = [
            s for s in ("429", "500", "503", "529") if not re.search(rf"\b{s}\b", text)
        ]
        if missing:
            fail("retry", f"No retry handling for status {', '.join(missing)}.")
        if not re.search(r"retry[-_]after", text, re.IGNORECASE):
            fail("retry", "retry-after header is not respected.")

        # 4. max_tokens on every messages call.
        if tree is not None:
            for call, kind in calls:
                if kind != "messages":
                    continue
                kws = {k.arg for k in call.keywords}
                if "max_tokens" in kws:
                    continue
                if None in kws and re.search(
                    r"""["']max_tokens["']|\bmax_tokens\s*=""", text
                ):
                    continue
                fail(
                    "max-tokens",
                    f"messages call at line {call.lineno} has no max_tokens.",
                )
        elif "max_tokens" not in text:
            fail("max-tokens", "No max_tokens found for the SDK call.")

        # 5. stop_reason / refusal handled.
        if "stop_reason" not in text or "refusal" not in text:
            fail("stop-reason", "stop_reason / refusal is not handled.")

    # 7. No raw content in logs under app/ai/.
    if tree is not None and AI_DIR.search(relpath):
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Attribute):
                continue
            if (
                n.func.attr not in LOG_METHODS
                or not _chain(n.func)[:1]
                or (_chain(n.func)[0] not in LOG_NAMES)
            ):
                continue
            refs = [
                r
                for a in [*n.args, *[k.value for k in n.keywords]]
                for r in _raw_refs(a)
            ]
            if refs:
                fail(
                    "log-content",
                    f"line {n.lineno} logs raw content ({', '.join(sorted(set(refs)))}); "
                    "log lengths or hashes only.",
                )
    return v


def _tracked_files(root: Path) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        out = [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()]
    return [p for p in out if p.endswith(SCAN_EXT) and not EXCLUDE.search(p)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument("files", nargs="*")
    args = ap.parse_args(argv)
    root = Path(args.root)
    allowed = load_allowed_models(root)
    files = [
        f
        for f in (args.files or _tracked_files(root))
        if f.endswith(SCAN_EXT) and not EXCLUDE.search(f)
    ]
    violations: list[Violation] = []
    checked = 0
    for rel in files:
        p = root / rel
        if not p.is_file():
            continue
        text = p.read_text(errors="replace")
        if is_detected(rel, text):
            checked += 1
        violations += check_file(rel, text, allowed)
    for item in violations:
        print(f"::error file={item.path}::{item}" if _in_actions() else item)
    print(f"claude-policy: {checked} file(s) checked, {len(violations)} violation(s)")
    return 1 if violations else 0


def _in_actions() -> bool:
    import os

    return os.environ.get("GITHUB_ACTIONS") == "true"


if __name__ == "__main__":
    sys.exit(main())

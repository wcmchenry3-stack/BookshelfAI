#!/usr/bin/env python3
"""Claude / Anthropic API policy check (#537). Stdlib only.

Mirrors ``.claude/policies/claude.md``. Scans tracked source files that touch
the Anthropic SDK or live under ``app/ai/`` and exits non-zero on any
violation. Run from anywhere: ``python3 scripts/policy/check_claude_policy.py``
(optionally ``--root DIR`` and explicit file paths relative to the root).

Checks 2, 4, 5, 7 and 8 are AST checks on Python code: comments, docstrings
and bare string statements never satisfy a requirement.

``check_file(relpath, text, allowed_models)`` is the pure entry point the tests
use; it never touches the disk.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DETECT = re.compile(
    r"from anthropic|import anthropic|@anthropic-ai/sdk|Anthropic\(|"
    r"ANTHROPIC_API_KEY|claude_agent_sdk|"
    r"\.messages\.(\w+\.)*(create|stream)\("
)
AI_DIR = re.compile(r"(^|/)app/ai/")
PROVIDERS_DIR = re.compile(r"(^|/)app/ai/providers/")
BACKEND_APP = re.compile(r"(^|/)backend/app/")
ENV_FILE = re.compile(r"(^|/)\.env")

SCAN_EXT = (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs")
# Prose, fixtures and the checker itself are not production code.
EXCLUDE = re.compile(
    r"(^|/)(node_modules|tests|__tests__|docs|\.claude|\.github|scripts/policy)/"
    r"|\.(test|spec)\.[jt]sx?$"
)
WALK_SKIP = frozenset({".git", ".venv", "venv", "site-packages", "node_modules"})

KEY = re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")
MODEL_ID = re.compile(r"""["'](claude-[A-Za-z0-9._-]+)["']""")
DATED = re.compile(r"-\d{8}$")
SDK_TEXT = re.compile(
    r"\.messages\.(?:\w+\.)*(?:create|stream)\(|\bAsyncAnthropic\(|\bAnthropic\("
)
RETRY_STATUSES = (429, 500, 503, 529)
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


def _fail_closed(msg: str) -> SystemExit:
    return SystemExit(f"claude-policy: {msg}")


def _unwrap(node: ast.expr) -> ast.expr:
    """``MappingProxyType({...})`` / ``frozenset({...})`` -> the literal."""
    while isinstance(node, ast.Call) and len(node.args) == 1 and not node.keywords:
        node = node.args[0]
    return node


def load_allowed_models(root: Path) -> frozenset[str]:
    """The approved Claude IDs: ``ALLOWED_MODELS["anthropic"]`` in
    ``backend/app/ai/models_allowlist.py``, read without importing the app.
    Only the module-level ``ALLOWED_MODELS`` assignment is read; anything
    unexpected fails closed with a one-line error."""
    path = root / "backend/app/ai/models_allowlist.py"
    try:
        tree = ast.parse(path.read_text())
    except (OSError, SyntaxError, ValueError) as exc:
        raise _fail_closed(f"cannot read {path.name}: {type(exc).__name__}") from None
    value = None
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            targets, val = stmt.targets, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets, val = [stmt.target], stmt.value
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == "ALLOWED_MODELS" for t in targets):
            value = val
    if value is None:
        raise _fail_closed("ALLOWED_MODELS is not assigned in models_allowlist.py")
    table = _unwrap(value)
    if not isinstance(table, ast.Dict):
        raise _fail_closed("ALLOWED_MODELS is not a dict literal")
    for k, v in zip(table.keys, table.values):
        if isinstance(k, ast.Constant) and k.value == "anthropic":
            items = _unwrap(v)
            if isinstance(items, (ast.Set, ast.List, ast.Tuple)) and all(
                isinstance(e, ast.Constant) and isinstance(e.value, str)
                for e in items.elts
            ):
                return frozenset(e.value for e in items.elts)
            raise _fail_closed(
                "ALLOWED_MODELS['anthropic'] is not a literal set of strings"
            )
    raise _fail_closed("ALLOWED_MODELS has no 'anthropic' key")


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
    """ "messages" for calls that take max_tokens, "other" for the rest of the
    SDK surface (client constructors, batches), None for non-SDK calls."""
    chain = _chain(node.func)
    if (
        len(chain) >= 2
        and chain[-1] in ("create", "stream")
        and "messages" in chain[:-1]
    ):
        if chain[-2] in ("messages", "with_raw_response", "with_streaming_response"):
            return "messages"
        return "other"
    if chain and chain[-1] in ("AsyncAnthropic", "Anthropic"):
        return "other"
    return None


def _py_sdk_calls(tree: ast.AST) -> list[tuple[ast.Call, str]]:
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            kind = _is_sdk_call(n)
            if kind:
                out.append((n, kind))
    return out


@dataclass
class CodeFacts:
    ints: set[int]
    strings: set[str]
    names: set[str]  # identifiers, attributes, function/arg/keyword names (lowercased)


def code_facts(tree: ast.AST) -> CodeFacts:
    """Facts from executable code only: docstrings and bare string statements
    are dropped, so a comment or docstring can never satisfy a check."""
    ignored: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant):
            ignored.add(id(n.value))
    facts = CodeFacts(set(), set(), set())
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and id(n) not in ignored:
            if isinstance(n.value, bool):
                continue
            if isinstance(n.value, int):
                facts.ints.add(n.value)
            elif isinstance(n.value, str):
                facts.strings.add(n.value)
        elif isinstance(n, ast.Name):
            facts.names.add(n.id.lower())
        elif isinstance(n, ast.Attribute):
            facts.names.add(n.attr.lower())
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            facts.names.add(n.name.lower())
        elif isinstance(n, (ast.arg, ast.keyword)) and n.arg:
            facts.names.add(n.arg.lower())
    return facts


SAFE_ATTRS = frozenset(
    {"width", "height", "size", "nbytes", "status_code", "mode", "format"}
)


def _root_name(node: ast.AST) -> str | None:
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node.id if isinstance(node, ast.Name) else None


def _raw_refs(node: ast.AST) -> list[str]:
    """Raw content a logging argument would emit: a bare raw-field name, an
    attribute whose own name is a raw field (``response.content``), a method
    call on a raw-named receiver (``text.strip()``), any attribute read on a
    raw-named base other than a few size-like ones (``image.width``,
    ``raw.status_code``, ``*_tokens``), or a raw-field subscript. Anything
    wrapped in len()/sha256()/hexdigest() is fine."""
    found: list[str] = []

    def visit(n: ast.AST, is_func: bool = False) -> None:
        if isinstance(n, ast.Call):
            if _call_name(n.func) in SAFE_WRAPPERS:
                return
            visit(n.func, True)
            for child in [*n.args, *[k.value for k in n.keywords]]:
                visit(child)
            return
        if isinstance(n, ast.Attribute):
            root = _root_name(n.value)
            safe_read = n.attr in SAFE_ATTRS or n.attr.endswith("_tokens")
            if n.attr in RAW_FIELDS or (
                root in RAW_FIELDS and (is_func or not safe_read)
            ):
                found.append(n.attr)
            if not isinstance(n.value, ast.Name):
                visit(n.value)
            return
        if isinstance(n, ast.Name):
            if n.id in RAW_FIELDS:
                found.append(n.id)
            return
        if (
            isinstance(n, ast.Subscript)
            and isinstance(n.slice, ast.Constant)
            and n.slice.value in RAW_FIELDS
        ):
            found.append(str(n.slice.value))
        for child in ast.iter_child_nodes(n):
            visit(child)

    visit(node)
    return found


def _dict_has_max_tokens(node: ast.AST | None, tree: ast.AST, depth: int = 0) -> bool:
    """Resolve ``node`` to a dict literal / ``dict(...)`` / the return values of
    a same-file function it calls, and require the ``max_tokens`` key."""
    if depth > 3 or node is None:
        return False
    if isinstance(node, ast.Dict):
        return any(
            isinstance(k, ast.Constant) and k.value == "max_tokens" for k in node.keys
        )
    if isinstance(node, ast.Call):
        name = _call_name(node.func)
        if name == "dict":
            return any(k.arg == "max_tokens" for k in node.keywords)
        returns = [
            r.value
            for f in ast.walk(tree)
            if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name == name
            for r in ast.walk(f)
            if isinstance(r, ast.Return) and r.value is not None
        ]
        return bool(returns) and all(
            _dict_has_max_tokens(r, tree, depth + 1) for r in returns
        )
    return False


def _splat_has_max_tokens(value: ast.expr, tree: ast.AST) -> bool:
    """``**value`` passes max_tokens when every same-file binding of the name
    resolves to a dict that has it; unresolvable fails closed."""
    if not isinstance(value, ast.Name):
        return _dict_has_max_tokens(value, tree)
    bound = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None:
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            if any(isinstance(t, ast.Name) and t.id == value.id for t in targets):
                bound.append(n.value)
    return bool(bound) and all(_dict_has_max_tokens(b, tree) for b in bound)


def _is_503(node: ast.expr | None) -> bool:
    if isinstance(node, ast.Constant):
        return node.value == 503
    if isinstance(node, ast.Attribute):
        return node.attr.endswith("SERVICE_UNAVAILABLE")
    if isinstance(node, ast.Name):
        return node.id.endswith("SERVICE_UNAVAILABLE")
    return False


def _budget_violations(tree: ast.AST) -> list[int]:
    """Lines of HTTPException-style calls with detail "ai_budget_reached"
    whose status is not 503."""
    bad = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        kws = {k.arg: k.value for k in n.keywords if k.arg}
        status, detail = kws.get("status_code"), kws.get("detail")
        if _call_name(n.func).endswith("HTTPException"):
            # Positional status and detail resolve independently, so
            # HTTPException(503, detail=...) is read correctly.
            if status is None and len(n.args) >= 1:
                status = n.args[0]
            if detail is None and len(n.args) >= 2:
                detail = n.args[1]
        is_budget = (
            isinstance(detail, ast.Constant) and detail.value == "ai_budget_reached"
        )
        if is_budget and not _is_503(status):
            bad.append(n.lineno)
    return bad


def check_file(
    relpath: str, text: str, allowed_models: frozenset[str]
) -> list[Violation]:
    is_py = relpath.endswith(".py")
    detected = is_detected(relpath, text)
    in_app = (
        is_py and bool(BACKEND_APP.search(relpath)) and not ENV_FILE.search(relpath)
    )
    if not detected and not in_app:
        return []
    v: list[Violation] = []

    def fail(check: str, msg: str) -> None:
        v.append(Violation(relpath, check, msg))

    tree: ast.AST | None = None
    if is_py:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            fail("parse", "file does not parse; the policy cannot be verified")
            return v

    # 8. Budget breaker refusal is 503 (checked on all of backend/app).
    if in_app and tree is not None:
        for line in _budget_violations(tree):
            fail(
                "budget-status",
                f"line {line}: ai_budget_reached must be refused with 503.",
            )

    if not detected:
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

    # SDK calls.
    if tree is not None:
        calls = _py_sdk_calls(tree)
        facts = code_facts(tree)
    else:
        calls = [(None, "other") for _ in SDK_TEXT.finditer(text)]
        facts = None
    has_sdk = bool(calls)

    # 6. SDK only under app/ai/providers/.
    if has_sdk and not PROVIDERS_DIR.search(relpath):
        fail(
            "sdk-location",
            "Anthropic SDK calls are only allowed under backend/app/ai/providers/.",
        )

    if has_sdk:
        # 2. Retry on 429/500/503/529 with retry-after.
        if facts is not None:
            missing = [str(s) for s in RETRY_STATUSES if s not in facts.ints]
            retry_after = any("retry-after" in s.lower() for s in facts.strings) or any(
                "retry_after" in n for n in facts.names
            )
        else:
            missing = [
                str(s) for s in RETRY_STATUSES if not re.search(rf"\b{s}\b", text)
            ]
            retry_after = bool(re.search(r"retry[-_]after", text, re.IGNORECASE))
        if missing:
            fail("retry", f"No retry handling for status {', '.join(missing)}.")
        if not retry_after:
            fail("retry", "retry-after header is not respected.")

        # 4. max_tokens on every messages call.
        if tree is not None:
            for call, kind in calls:
                if kind != "messages":
                    continue
                kws = {k.arg for k in call.keywords}
                if "max_tokens" in kws:
                    continue
                splats = [k.value for k in call.keywords if k.arg is None]
                if splats and all(_splat_has_max_tokens(v, tree) for v in splats):
                    continue
                fail(
                    "max-tokens",
                    f"messages call at line {call.lineno} has no max_tokens.",
                )
        elif "max_tokens" not in text:
            fail("max-tokens", "No max_tokens found for the SDK call.")

        # 5. stop_reason / refusal handled.
        if facts is not None:
            handled = (
                "stop_reason" in facts.names
                or "stop_reason" in facts.strings
                or ("refusal" in facts.strings)
            )
        else:
            handled = "stop_reason" in text or "refusal" in text
        if not handled:
            fail("stop-reason", "stop_reason / refusal is not handled.")

    # 7. No raw content in logs under app/ai/.
    if tree is not None and AI_DIR.search(relpath):
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Attribute):
                continue
            chain = _chain(n.func)
            if n.func.attr not in LOG_METHODS or chain[0] not in LOG_NAMES:
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
        out = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in WALK_SKIP]
            out += [str((Path(dirpath) / f).relative_to(root)) for f in filenames]
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
        found = check_file(rel, text, allowed)
        if is_detected(rel, text) or found:
            checked += 1
        violations += found
    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    for item in violations:
        print(f"::error file={item.path}::{item}" if in_actions else item)
    print(f"claude-policy: {checked} file(s) checked, {len(violations)} violation(s)")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Flag destructive operations in Alembic migrations (#687).

Usage::

    python3 scripts/migration_guard/check_destructive_migrations.py [--approved] FILE...

Only the body of ``upgrade()`` is inspected (with ``ast``, not regex over the
file); ``downgrade()`` is ignored. Each finding is printed as
``file:line: operation`` and the exit code is 1 when there is any finding,
unless ``--approved`` is given (the PR carries the
``destructive-migration-approved`` label), which prints them and exits 0.

Flagged (``op.`` and the ``batch_op.`` of ``with op.batch_alter_table(...)``):

- ``drop_table``, ``drop_column``, ``rename_table``
- ``alter_column`` with ``new_column_name`` (rename), ``type_`` (any type
  change; static narrowing detection is unreliable, so every change needs
  approval) or ``nullable=False`` (tightening)
- ``execute`` whose string literals (``sa.text`` included) contain DROP TABLE,
  DROP COLUMN, TRUNCATE, DELETE or ALTER ... TYPE (case-insensitive), or that
  build a ``delete(...)`` construct

Allowed on purpose: dropping an index or a constraint (no data is lost), and
anything additive. Operations reached through a helper function called from
``upgrade()`` are not followed; keep destructive steps inline.

Stdlib only; runs on any Python 3.9+.
"""

from __future__ import annotations

import ast
import re
import sys

APPROVAL_LABEL = "destructive-migration-approved"

_SQL = re.compile(
    r"\bDROP\s+(TABLE|COLUMN)\b|\bTRUNCATE\b|\bDELETE\b|\bALTER\b.*?\bTYPE\b",
    re.IGNORECASE | re.DOTALL,
)
_ALWAYS = {"drop_table", "drop_column", "rename_table"}


def _batch_names(func: ast.FunctionDef) -> set[str]:
    """Names bound by ``with op.batch_alter_table(...) as name``."""
    names: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                call = item.context_expr
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "batch_alter_table"
                    and isinstance(item.optional_vars, ast.Name)
                ):
                    names.add(item.optional_vars.id)
    return names


def _is_delete_construct(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    return (isinstance(f, ast.Name) and f.id == "delete") or (
        isinstance(f, ast.Attribute) and f.attr == "delete"
    )


def _classify(call: ast.Call, method: str) -> str | None:
    """Return a short description of the destructive operation, or None."""
    if method in _ALWAYS:
        return method
    if method == "alter_column":
        for kw in call.keywords:
            if kw.arg == "new_column_name":
                return "alter_column(new_column_name=...) renames a column"
            if kw.arg == "type_":
                return "alter_column(type_=...) changes a column type"
            if (
                kw.arg == "nullable"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is False
            ):
                return "alter_column(nullable=False) tightens a column"
        return None
    if method == "execute":
        values = [*call.args, *[k.value for k in call.keywords]]
        for node in (n for v in values for n in ast.walk(v)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if _SQL.search(node.value):
                    return "execute() with destructive SQL"
            if _is_delete_construct(node):
                return "execute() deleting rows"
    return None


def scan_source(source: str, path: str) -> list[str]:
    """Return ``path:line: operation`` findings for the ``upgrade()`` body."""
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return [f"{path}:{exc.lineno or 1}: cannot parse migration ({exc.msg})"]
    findings: list[tuple[int, str]] = []
    for func in tree.body:
        if not (isinstance(func, ast.FunctionDef) and func.name == "upgrade"):
            continue
        targets = {"op"} | _batch_names(func)
        for node in ast.walk(func):
            if not (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            ):
                continue
            base = node.func.value
            if not (isinstance(base, ast.Name) and base.id in targets):
                continue
            what = _classify(node, node.func.attr)
            if what:
                findings.append((node.lineno, f"{base.id}.{what}"))
    return [f"{path}:{line}: {text}" for line, text in sorted(findings)]


def main(argv: list[str]) -> int:
    approved = "--approved" in argv
    files = [a for a in argv if a != "--approved"]
    findings: list[str] = []
    for path in files:
        if not path.endswith(".py"):
            continue
        with open(path, encoding="utf-8") as fh:
            findings += scan_source(fh.read(), path)
    for line in findings:
        print(line)
    if not findings:
        print("No destructive migration operations found.")
        return 0
    if approved:
        print(
            f"Destructive operations found; approved by the '{APPROVAL_LABEL}' label."
        )
        return 0
    print(
        f"\nDestructive migration operations need the '{APPROVAL_LABEL}' label "
        "(applied by the owner only). See docs/claude/backend.md, 'Migrations: expand/contract'."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

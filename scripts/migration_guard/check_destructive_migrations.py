#!/usr/bin/env python3
"""Flag destructive operations in Alembic migrations (#687).

Usage::

    python3 scripts/migration_guard/check_destructive_migrations.py [--approved] FILE...

The body of ``upgrade()`` and the module-level helpers it calls (transitively)
are inspected with ``ast``, not regex over the file; ``downgrade()`` is ignored.
Each finding is printed as
``file:line: operation`` and the exit code is 1 when there is any finding,
unless ``--approved`` is given (the PR carries the
``destructive-migration-approved`` label), which prints them and exits 0.

Flagged (``op.`` and the ``batch_op.`` of ``with op.batch_alter_table(...)``):

- ``drop_table``, ``drop_column``, ``rename_table``
- ``alter_column`` with ``new_column_name`` (rename), ``type_`` (any type
  change; static narrowing detection is unreliable, so every change needs
  approval) or ``nullable=False`` (tightening)
- ``.execute(...)`` on any receiver, or a call to a followed helper, whose
  strings (module constants, ``+`` and f-strings resolved) contain DROP
  TABLE/COLUMN/SCHEMA/TYPE/VIEW, TRUNCATE, DELETE FROM, ALTER TABLE ... RENAME or
  ALTER ... TYPE (case-insensitive), or that build a ``delete(...)`` construct
- a file with no top-level ``upgrade()`` (fails closed)

Allowed on purpose: dropping an index or a constraint (no data is lost), and
anything additive. SQL built at run time from values the file does not define
is not seen.

Not understood (not flagged): ``import alembic; alembic.op.X(...)``,
``from alembic.op import X``, and SQL assembled with calls such as
``" ".join([...])``.

Stdlib only; runs on any Python 3.9+.
"""

from __future__ import annotations

import ast
import re
import sys

APPROVAL_LABEL = "destructive-migration-approved"

_SQL = re.compile(
    r"\bDROP\s+(TABLE|COLUMN|SCHEMA|TYPE|VIEW)\b"
    r"|\bTRUNCATE\b"
    r"|\bDELETE\s+FROM\b"
    r"|\bALTER\s+TABLE\b[^;]*?\bRENAME\b"
    r"|\bALTER\s+COLUMN\b[^;]*?\bTYPE\b"
    r"|\bALTER\s+TABLE\b[^;]*?\bALTER\b[^;]*?\bTYPE\b",
    re.IGNORECASE | re.DOTALL,
)
_ALWAYS = {"drop_table", "drop_column", "rename_table"}


def _op_names(tree: ast.Module) -> set[str]:
    """``op`` plus any alias from ``from alembic import op as X``."""
    names = {"op"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "alembic":
            names |= {a.asname for a in node.names if a.name == "op" and a.asname}
    return names


def _batch_names(func: ast.FunctionDef, ops: set[str]) -> set[str]:
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


def _constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` (also ``+`` concatenations of them)."""
    consts: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                text = _render(node.value, consts)
                if text is not None:
                    consts[target.id] = text
    return consts


def _render(node: ast.AST, consts: dict[str, str]) -> str | None:
    """Static string value of a literal, constant name, ``+`` chain or f-string."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _render(node.left, consts), _render(node.right, consts)
        if left is not None and right is not None:
            return left + right
    if (
        isinstance(node, ast.Call)
        and len(node.args) == 1
        and not node.keywords
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "text")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "text")
        )
    ):
        return _render(node.args[0], consts)  # sa.text("...") is its SQL
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.FormattedValue):
                parts.append(_render(v.value, consts) or "x")
            else:
                parts.append(_render(v, consts) or "")
        return "".join(parts)
    return None


def _texts(node: ast.AST, consts: dict[str, str]):
    """Every static string reachable in an expression."""
    text = _render(node, consts)
    if text is not None:
        yield text
    else:
        for child in ast.iter_child_nodes(node):
            yield from _texts(child, consts)


def _with_locals(func: ast.FunctionDef, consts: dict[str, str]) -> dict[str, str]:
    """Module constants plus simple ``name = <static string>`` inside ``func``.

    A name assigned more than once keeps every value it is given (joined with
    ``;`` so patterns don't span them): resolving each call against only the
    last value would let ``sql = "DROP …"; op.execute(sql); sql = "SELECT 1"``
    through. This over-approximates, which fails safe (the label clears it).
    """
    merged = dict(consts)
    seen: set[str] = set()
    assigns = [n for n in ast.walk(func) if isinstance(n, ast.Assign)]
    for node in sorted(assigns, key=lambda n: n.lineno):
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            text = _render(node.value, merged)
            if text is None:
                continue
            if name in seen:
                text = f"{merged[name]}\n;\n{text}"
            merged[name] = text
            seen.add(name)
    return merged


def _is_delete_construct(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    return (isinstance(f, ast.Name) and f.id == "delete") or (
        isinstance(f, ast.Attribute) and f.attr == "delete"
    )


def _args(call: ast.Call) -> list[ast.AST]:
    return [*call.args, *[k.value for k in call.keywords]]


def _sql_hit(call: ast.Call, consts: dict[str, str]) -> str | None:
    for arg in _args(call):
        if any(_SQL.search(t) for t in _texts(arg, consts)):
            return "destructive SQL"
        if any(_is_delete_construct(n) for n in ast.walk(arg)):
            return "deleting rows"
    return None


def _classify(call: ast.Call, method: str) -> str | None:
    """Description of a destructive ``op``/``batch_op`` schema call, or None."""
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


def scan_source(source: str, path: str) -> list[str]:
    """Return ``path:line: operation`` findings for ``upgrade()`` and its helpers."""
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return [f"{path}:{exc.lineno or 1}: cannot parse migration ({exc.msg})"]
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    if "upgrade" not in funcs:
        return [f"{path}:1: no top-level upgrade() found (cannot check this migration)"]
    consts = _constants(tree)
    ops = _op_names(tree)
    findings: set[tuple[int, str]] = set()
    seen: set[str] = set()
    queue = ["upgrade"]
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        func = funcs[name]
        targets = ops | _batch_names(func, ops)
        scope = _with_locals(func, consts)
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if isinstance(f, ast.Name) and f.id in funcs:
                queue.append(f.id)  # follow module-level helpers, cycle-safe
                what = _sql_hit(node, scope)
                if what:
                    findings.add((node.lineno, f"{f.id}(): {what}"))
            elif isinstance(f, ast.Attribute):
                base = f.value
                if isinstance(base, ast.Name) and base.id in targets:
                    what = _classify(node, f.attr)
                    if what:
                        findings.add((node.lineno, f"{base.id}.{what}"))
                if f.attr == "execute":
                    what = _sql_hit(node, scope)
                    if what:
                        findings.add((node.lineno, f"execute(): {what}"))
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

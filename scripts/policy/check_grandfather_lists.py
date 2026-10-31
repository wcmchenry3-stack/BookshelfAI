#!/usr/bin/env python3
"""Grandfather-list policy (#820). Stdlib only.

A PR may not add an exception to a gate (the file-length allowlist, a knip
``ignore*`` key, a jscpd ``ignore`` entry) unless the same file's diff also adds
a line naming a linked ``epic:refactor`` issue.

CI's policy job sees neither the PR body nor the labels, so the reference has to
live in the diff itself:

- ``scripts/file_length_allowlist.txt``: ``path 700  # epic:refactor #123``
- ``frontend/knip.json``: a ``// epic:refactor #123`` comment (the file is JSONC)
- ``.jscpd.json`` / ``.jscpd.tests.json``: a ``"//": "epic:refactor #123"`` key

Usage: ``python3 scripts/policy/check_grandfather_lists.py --diff-base origin/dev``
(run from anywhere; ``--root DIR`` overrides the repo root). Lowering an
allowlist cap, or moving an existing entry, is not an addition.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

EPIC_REF = re.compile(r"epic:refactor\s+#\d+")
ALLOWLIST_FILE = "scripts/file_length_allowlist.txt"
IGNORE_KEY = re.compile(r'"(ignore\w*)"\s*:')
JSON_GRANDFATHER_FILES = ("frontend/knip.json", ".jscpd.json", ".jscpd.tests.json")
GRANDFATHER_FILES = (ALLOWLIST_FILE, *JSON_GRANDFATHER_FILES)
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')


@dataclass(frozen=True)
class Violation:
    path: str
    check: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: [{self.check}] {self.message}"


def _strip_jsonc_comment(line: str) -> str:
    """Drop a ``//`` comment that sits outside any string literal."""
    in_str = False
    i = 0
    while i < len(line):
        ch = line[i]
        if in_str:
            if ch == "\\":
                i += 1
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif line.startswith("//", i):
            return line[:i]
        i += 1
    return line


def ignore_entry_lines(text: str) -> dict[int, str]:
    """Lines (1-based) inside ``ignore*`` keys of a JSON/JSONC file that carry
    a string entry, mapped to their comment-stripped text."""
    found: dict[int, str] = {}
    depth = 0
    inside = False
    for n, raw in enumerate(text.splitlines(), 1):
        code = _strip_jsonc_comment(raw)
        key = IGNORE_KEY.search(code) if not inside else None
        if key:
            inside = True
            depth = 0
        if not inside:
            continue
        body = code
        if key:
            body = code[: key.start()] + code[key.end() :]
        if _STRING.search(body):
            found[n] = body.strip()
        stripped = _STRING.sub('""', code)
        depth += stripped.count("[") + stripped.count("{")
        depth -= stripped.count("]") + stripped.count("}")
        if depth <= 0:
            inside = False
    return found


def parse_diff(diff: str) -> dict[str, tuple[dict[int, str], list[str]]]:
    """Zero-context unified diff -> {path: ({new_line_no: added}, [removed])}."""
    files: dict[str, tuple[dict[int, str], list[str]]] = {}
    path = None
    new_no = 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            tgt = line[4:].strip()
            path = tgt[2:] if tgt.startswith("b/") else None
            if path is not None:
                files.setdefault(path, ({}, []))
            continue
        if line.startswith("--- ") or path is None:
            continue
        m = _HUNK.match(line)
        if m:
            new_no = int(m.group(1))
        elif line.startswith("+"):
            files[path][0][new_no] = line[1:]
            new_no += 1
        elif line.startswith("-"):
            files[path][1].append(line[1:])
    return files


def _allowlist_entry(text: str) -> tuple[str, int] | None:
    parts = text.split("#", 1)[0].split()
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0], int(parts[1])
    return None


def check_grandfather_diff(
    diff: str, read_new: Callable[[str], str | None]
) -> list[Violation]:
    """Violations for exceptions ``diff`` adds without an epic:refactor
    reference in the same file. ``read_new(path)`` returns the new file text."""
    out: list[Violation] = []
    for path, (added, removed) in parse_diff(diff).items():
        if path not in GRANDFATHER_FILES:
            continue
        offenders: list[str] = []
        if path == ALLOWLIST_FILE:
            old = {e[0]: e[1] for e in map(_allowlist_entry, removed) if e}
            for text in added.values():
                entry = _allowlist_entry(text)
                if entry and (entry[0] not in old or entry[1] > old[entry[0]]):
                    offenders.append(text.strip())
        else:
            new_text = read_new(path)
            if new_text is None:
                continue
            removed_norm = {_strip_jsonc_comment(r).strip() for r in removed}
            entries = ignore_entry_lines(new_text)
            for no, text in added.items():
                if no in entries and entries[no] not in removed_norm:
                    offenders.append(entries[no])
        if offenders and not any(EPIC_REF.search(t) for t in added.values()):
            out.append(
                Violation(
                    path,
                    "grandfather-list",
                    f"adds {len(offenders)} exception(s) (first: {offenders[0]!r}) "
                    "without a linked epic:refactor issue; fix the code instead, or "
                    "add a line containing 'epic:refactor #NNN' in the same file.",
                )
            )
    return out


def diff_against(root: Path, base: str) -> str:
    cmd = ["git", "diff", "-U0", "--no-color", f"{base}...HEAD", "--"]
    try:
        return subprocess.run(
            [*cmd, *GRANDFATHER_FILES],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        # Fail closed: an unreadable diff must not look like "no additions".
        raise SystemExit(f"grandfather-lists: cannot diff against {base}") from None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument("--diff-base", required=True, help="ref the PR branches from")
    args = ap.parse_args(argv)
    root = Path(args.root)

    def read_new(rel: str) -> str | None:
        p = root / rel
        return p.read_text(errors="replace") if p.is_file() else None

    violations = check_grandfather_diff(diff_against(root, args.diff_base), read_new)
    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    for v in violations:
        print(f"::error file={v.path}::{v}" if in_actions else v)
    print(f"grandfather-lists: {len(violations)} violation(s)")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())

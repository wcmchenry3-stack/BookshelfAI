#!/usr/bin/env python3
"""Grandfather-list policy (#820). Stdlib only.

A PR may not add an exception to a gate, or loosen one, unless the same file's
diff also adds a line naming a linked ``epic:refactor`` issue (or
``owner-approved #N`` for an owner-approved permanent exception).

What is compared is the *collection* of exceptions in the old and new version of
each guarded file, not diff lines, so deleting an entry, reordering, or lowering
a cap never flags, and renaming a key to ``ignore*`` does:

- ``scripts/file_length_allowlist.txt``: a new path, or a raised cap.
- knip config (``knip.json``, ``knip.jsonc``, ``.knip.json``, ``.knip.jsonc``,
  ``knip.ts``/``knip.js``/``knip.config.ts``/``knip.config.js``, the ``knip`` key
  of any ``package.json``): every ``ignore*`` key at any depth, as a set of
  (key path, value); any entry not in the old set. A knip ``.ts``/``.js`` config
  cannot be compared, so adding one is a violation.
- ``.jscpd*.json``: the same ``ignore*`` rule, plus a raised ``threshold``,
  ``minLines`` or ``minTokens`` (or a removed ``threshold``).

CI's policy job sees neither the PR body nor the labels, so the reference lives
in the diff: ``# epic:refactor #123`` on an allowlist line, ``// epic:refactor
#123`` in JSONC, or a ``"//": "epic:refactor #123"`` key in JSON. One reference
per file is enough; this is a speed bump that sits behind CODEOWNERS review, and
it does not check that the issue exists. Not covered: knip ``project`` negations,
source ignore comments, workflow flags.

Usage: ``python3 scripts/policy/check_grandfather_lists.py --diff-base origin/dev``
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MARKER = re.compile(r"(?:epic:refactor|owner-approved)\s+#\d+")
ALLOWLIST_FILE = "scripts/file_length_allowlist.txt"
KNIP_JSON = re.compile(r"(^|/)\.?knip\.jsonc?$")
KNIP_CODE = re.compile(r"(^|/)knip(\.config)?\.(ts|js)$")
JSCPD = re.compile(r"(^|/)\.jscpd[^/]*\.json$")
PACKAGE_JSON = re.compile(r"(^|/)package\.json$")
LOOSENING_KEYS = ("threshold", "minLines", "minTokens")


@dataclass(frozen=True)
class Violation:
    path: str
    check: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: [{self.check}] {self.message}"


def is_guarded(path: str) -> bool:
    return bool(
        path == ALLOWLIST_FILE
        or KNIP_JSON.search(path)
        or KNIP_CODE.search(path)
        or JSCPD.search(path)
        or PACKAGE_JSON.search(path)
    )


def parse_jsonc(text: str):
    """Parse JSON with ``//`` and ``/* */`` comments and trailing commas."""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            out.append(ch)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 1
        else:
            out.append(ch)
        i += 1
    return json.loads(_drop_trailing_commas("".join(out)))


def _drop_trailing_commas(text: str) -> str:
    """Remove a comma before ``}``/``]``, but never one inside a string."""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            out.append(ch)
        elif ch == ",":
            j = i + 1
            while j < n and text[j].isspace():
                j += 1
            if j >= n or text[j] not in "}]":
                out.append(ch)
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _flatten(prefix: tuple, value) -> set[tuple]:
    if isinstance(value, dict):
        found: set[tuple] = set()
        for k, v in value.items():
            found |= _flatten((*prefix, k), v)
        return found
    if isinstance(value, list):
        return {(prefix, json.dumps(v, sort_keys=True)) for v in value}
    return {(prefix, json.dumps(value, sort_keys=True))}


def ignore_entries(data, prefix: tuple = ()) -> set[tuple]:
    """Every entry under an ``ignore*`` key at any depth, as (key path, value)."""
    found: set[tuple] = set()
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(k, str) and k.startswith("ignore"):
                found |= _flatten((*prefix, k), v)
            else:
                found |= ignore_entries(v, (*prefix, k))
    elif isinstance(data, list):
        for v in data:
            found |= ignore_entries(v, prefix)
    return found


def _allowlist(text: str | None) -> dict[str, int]:
    caps: dict[str, int] = {}
    for raw in (text or "").splitlines():
        parts = raw.split("#", 1)[0].split()
        if len(parts) == 2 and parts[1].isdigit():
            caps[parts[0]] = int(parts[1])
    return caps


def _config(path: str, text: str | None):
    if text is None:
        return {}
    data = parse_jsonc(text)
    if PACKAGE_JSON.search(path):
        data = data.get("knip", {}) if isinstance(data, dict) else {}
    return data


def new_exceptions(path: str, old: str | None, new: str | None) -> list[str]:
    """Human-readable exceptions present in ``new`` but not in ``old``."""
    if new is None:
        return []
    if path == ALLOWLIST_FILE:
        before, after = _allowlist(old), _allowlist(new)
        return [
            f"{p} {c}" for p, c in after.items() if p not in before or c > before[p]
        ]
    if KNIP_CODE.search(path):
        return [] if old is not None else [f"{path} (knip code config added)"]
    try:
        before_cfg, after_cfg = _config(path, old), _config(path, new)
    except ValueError as exc:
        return [f"unparseable JSON ({exc})"]
    found = [
        f"{'.'.join(map(str, key))}: {val}"
        for key, val in sorted(
            ignore_entries(after_cfg) - ignore_entries(before_cfg), key=str
        )
    ]
    if JSCPD.search(path) and isinstance(after_cfg, dict):
        old_cfg = before_cfg if isinstance(before_cfg, dict) else {}
        for key in LOOSENING_KEYS:
            was, now = old_cfg.get(key), after_cfg.get(key)
            if old is None or was is None:
                continue
            if now is None and key == "threshold":
                found.append("threshold removed")
            elif isinstance(now, (int, float)) and now > was:
                found.append(f"{key} raised {was} -> {now}")
    return found


def check_changes(changes: dict[str, tuple]) -> list[Violation]:
    """``changes``: {path: (old_text | None, new_text | None, added_lines)}."""
    out: list[Violation] = []
    for path, (old, new, added) in sorted(changes.items()):
        if not is_guarded(path):
            continue
        found = new_exceptions(path, old, new)
        if found and not any(MARKER.search(line) for line in added):
            out.append(
                Violation(
                    path,
                    "grandfather-list",
                    f"adds {len(found)} exception(s) or loosening (first: {found[0]!r}) "
                    "without a linked epic:refactor issue; fix the code instead, or "
                    "add a line containing 'epic:refactor #NNN' in the same file.",
                )
            )
    return out


def _run(root: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        # Fail closed: an unreadable diff must not look like "no additions".
        raise SystemExit(f"grandfather-lists: git {args[0]} failed") from None


def collect_changes(root: Path, base: str) -> dict[str, tuple]:
    merge_base = _run(root, "merge-base", base, "HEAD").strip()
    names = _run(
        root, "diff", "--name-only", "--diff-filter=ACMR", f"{merge_base}...HEAD"
    ).splitlines()
    changes: dict[str, tuple] = {}
    for path in filter(is_guarded, names):
        try:
            old = _run(root, "show", f"{merge_base}:{path}")
        except SystemExit:
            old = None
        target = root / path
        new = target.read_text(errors="replace") if target.is_file() else None
        diff = _run(
            root, "diff", "-U0", "--no-color", f"{merge_base}...HEAD", "--", path
        )
        added = [
            ln[1:]
            for ln in diff.splitlines()
            if ln.startswith("+") and not ln.startswith("+++")
        ]
        changes[path] = (old, new, added)
    return changes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument("--diff-base", required=True, help="ref the PR branches from")
    args = ap.parse_args(argv)
    violations = check_changes(collect_changes(Path(args.root), args.diff_base))
    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    for v in violations:
        print(f"::error file={v.path}::{v}" if in_actions else v)
    print(f"grandfather-lists: {len(violations)} violation(s)")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())

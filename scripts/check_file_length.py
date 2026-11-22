#!/usr/bin/env python3
"""Fail when a source file (.py, .ts, .tsx) grows past the line cap.

Stdlib only. Run from anywhere: ``python3 scripts/check_file_length.py``.

Files over ``DEFAULT_MAX_LINES`` must be listed in
``scripts/file_length_allowlist.txt`` (``path max_lines``, one per line,
``#`` comments allowed). An allowlisted file may shrink but not grow past its
own cap. Stale entries (file now under the cap, or far below its own cap) are
reported as warnings so the allowlist gets tightened; ``--strict`` makes them
fail too.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

DEFAULT_MAX_LINES = 600
EXTENSIONS = (".py", ".ts", ".tsx")
EXCLUDED_DIRS = {
    "node_modules",
    "ios",
    "android",
    "tests",
    "test",
    "__tests__",
    "locales",
    "generated",
    "__generated__",
    "dist",
    "build",
    "__pycache__",
}
EXCLUDED_PREFIXES = ("backend/alembic/versions/",)
EXCLUDED_SUFFIXES = (
    ".d.ts",
    ".test.ts",
    ".test.tsx",
    ".spec.ts",
    ".spec.tsx",
    ".generated.ts",
)
ALLOWLIST_RELPATH = "scripts/file_length_allowlist.txt"


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def is_excluded(rel: str) -> bool:
    parts = rel.split("/")
    if any(p in EXCLUDED_DIRS for p in parts[:-1]):
        return True
    name = parts[-1]
    if (
        name.endswith(EXCLUDED_SUFFIXES)
        or name.startswith("test_")
        or name == "conftest.py"
    ):
        return True
    return rel.startswith(EXCLUDED_PREFIXES)


def count_lines(path: Path) -> int:
    data = path.read_bytes()
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def find_sources(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        dirnames[:] = sorted(
            d for d in dirnames if d not in EXCLUDED_DIRS and not d.startswith(".")
        )
        for fn in sorted(filenames):
            if not fn.endswith(EXTENSIONS):
                continue
            rel = fn if rel_dir == "." else f"{rel_dir}/{fn}"
            if not is_excluded(rel):
                yield rel


def load_allowlist(path: Path) -> dict[str, int]:
    caps: dict[str, int] = {}
    if not path.exists():
        return caps
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2 or not parts[1].isdigit():
            raise ValueError(
                f"{path.name}:{n}: expected '<path> <max_lines>', got {raw!r}"
            )
        caps[parts[0]] = int(parts[1])
    return caps


def check(root: Path, allowlist: dict[str, int], limit: int = DEFAULT_MAX_LINES):
    """Return (violations, stale) as lists of human-readable strings."""
    violations: list[str] = []
    stale: list[str] = []
    seen: set[str] = set()
    for rel in find_sources(root):
        seen.add(rel)
        n = count_lines(root / rel)
        cap = allowlist.get(rel)
        if cap is None:
            if n > limit:
                violations.append(
                    f"{rel}: {n} lines exceeds the {limit}-line cap "
                    f"(split it, or as a last resort add '{rel} {n}' to {ALLOWLIST_RELPATH})"
                )
        elif n > cap:
            violations.append(f"{rel}: {n} lines exceeds its allowlist cap of {cap}")
        elif n <= limit:
            stale.append(
                f"{rel}: {n} lines is under {limit}; remove it from the allowlist"
            )
        elif n < cap:
            stale.append(
                f"{rel}: {n} lines is under its cap of {cap}; lower the cap to {n}"
            )
    for rel in allowlist:
        if rel not in seen:
            stale.append(
                f"{rel}: allowlisted but not found; remove it from the allowlist"
            )
    return violations, stale


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Fail when a source file exceeds the line cap."
    )
    ap.add_argument("--root", type=Path, default=repo_root())
    ap.add_argument("--allowlist", type=Path, default=None)
    ap.add_argument("--limit", type=int, default=DEFAULT_MAX_LINES)
    ap.add_argument(
        "--strict", action="store_true", help="fail on stale allowlist entries too"
    )
    args = ap.parse_args(argv)
    root = args.root.resolve()
    allow_path = args.allowlist or (root / ALLOWLIST_RELPATH)
    try:
        allowlist = load_allowlist(allow_path)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    violations, stale = check(root, allowlist, args.limit)
    for s in stale:
        print(f"warning: {s}")
    for v in violations:
        print(f"error: {v}")
    if violations or (args.strict and stale):
        return 1
    print(f"file-length check passed (cap {args.limit}, {len(allowlist)} allowlisted)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

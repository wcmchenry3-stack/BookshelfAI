"""Unit tests for scripts/check_file_length.py (repo-hygiene file-length cap, #769).

The script lives outside the backend package, so it is imported by path.
"""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check_file_length.py"
_spec = importlib.util.spec_from_file_location("check_file_length", SCRIPT)
cfl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cfl)


def _write(root: Path, rel: str, lines: int) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x = 1\n" * lines, encoding="utf-8")


def test_file_at_cap_passes_and_over_cap_fails(tmp_path):
    _write(tmp_path, "backend/app/ok.py", 600)
    _write(tmp_path, "backend/app/big.py", 601)
    violations, stale = cfl.check(tmp_path, {})
    assert len(violations) == 1
    assert "backend/app/big.py: 601 lines" in violations[0]
    assert stale == []


@pytest.mark.parametrize(
    "rel",
    [
        "backend/tests/unit/test_big.py",
        "frontend/__tests__/big.test.tsx",
        "frontend/lib/foo.test.ts",
        "frontend/i18n/locales/en.ts",
        "backend/alembic/versions/0001_big.py",
        "frontend/node_modules/pkg/index.ts",
        "frontend/ios/Pods/x.ts",
        "frontend/android/app/x.ts",
        "frontend/types/api.d.ts",
        "frontend/lib/api.generated.ts",
        "backend/conftest.py",
    ],
)
def test_excluded_paths_are_ignored(tmp_path, rel):
    _write(tmp_path, rel, 700)
    assert cfl.check(tmp_path, {}) == ([], [])


def test_non_source_extensions_are_ignored(tmp_path):
    (tmp_path / "data.json").write_text("{}\n" * 900)
    (tmp_path / "notes.md").write_text("x\n" * 900)
    assert cfl.check(tmp_path, {}) == ([], [])


def test_allowlisted_file_may_shrink_but_not_grow(tmp_path):
    _write(tmp_path, "backend/app/big.py", 700)
    assert cfl.check(tmp_path, {"backend/app/big.py": 700}) == ([], [])

    _write(tmp_path, "backend/app/big.py", 701)
    violations, _ = cfl.check(tmp_path, {"backend/app/big.py": 700})
    assert violations and "allowlist cap of 700" in violations[0]

    _write(tmp_path, "backend/app/big.py", 650)
    violations, stale = cfl.check(tmp_path, {"backend/app/big.py": 700})
    assert violations == []
    assert stale and "lower the cap to 650" in stale[0]


def test_stale_allowlist_entries_are_reported(tmp_path):
    _write(tmp_path, "backend/app/small.py", 10)
    _, stale = cfl.check(
        tmp_path, {"backend/app/small.py": 700, "backend/app/gone.py": 900}
    )
    assert any("small.py" in s and "remove it" in s for s in stale)
    assert any("gone.py" in s and "not found" in s for s in stale)


def test_load_allowlist_parses_comments_and_rejects_garbage(tmp_path):
    f = tmp_path / "allow.txt"
    f.write_text("# header\n\na/b.py 700  # why\n")
    assert cfl.load_allowlist(f) == {"a/b.py": 700}
    f.write_text("a/b.py notanumber\n")
    with pytest.raises(ValueError):
        cfl.load_allowlist(f)


def test_main_exit_codes(tmp_path, capsys):
    _write(tmp_path, "backend/app/big.py", 601)
    (tmp_path / "scripts").mkdir()
    assert cfl.main(["--root", str(tmp_path)]) == 1
    (tmp_path / "scripts" / "file_length_allowlist.txt").write_text(
        "backend/app/big.py 601\n"
    )
    assert cfl.main(["--root", str(tmp_path)]) == 0
    assert "passed" in capsys.readouterr().out


def test_repo_allowlist_is_current():
    """The real repo passes the strict check (#820): no long files, no stale entries."""
    root = SCRIPT.parent.parent
    assert cfl.main(["--root", str(root), "--strict"]) == 0


def test_repo_allowlist_entries_carry_a_marker():
    """#820: the allowlist is empty today; any entry must be an approved exception.

    Each entry line needs an ``epic:refactor #N`` or ``owner-approved #N`` marker.
    """
    import re

    marker = re.compile(r"(epic:refactor|owner-approved)\s+#\d+")
    path = SCRIPT.parent.parent / cfl.ALLOWLIST_RELPATH
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.split("#", 1)[0].strip():
            assert marker.search(raw), f"allowlist entry without a marker: {raw!r}"


def test_strict_fails_on_stale_entry(tmp_path, capsys):
    _write(tmp_path, "backend/app/small.py", 10)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "file_length_allowlist.txt").write_text(
        "backend/app/small.py 700\n"
    )
    assert cfl.main(["--root", str(tmp_path)]) == 0  # lenient: warning only
    assert cfl.main(["--root", str(tmp_path), "--strict"]) == 1
    assert "remove it from the allowlist" in capsys.readouterr().out


def test_strict_fails_on_stale_cap_and_missing_file(tmp_path):
    _write(tmp_path, "backend/app/big.py", 650)
    (tmp_path / "scripts").mkdir()
    allow = tmp_path / "scripts" / "file_length_allowlist.txt"
    allow.write_text("backend/app/big.py 700\n")
    assert cfl.main(["--root", str(tmp_path), "--strict"]) == 1
    allow.write_text("backend/app/big.py 650\nbackend/app/gone.py 900\n")
    assert cfl.main(["--root", str(tmp_path), "--strict"]) == 1


def test_strict_fails_on_over_cap_file(tmp_path, capsys):
    _write(tmp_path, "backend/app/big.py", 601)
    (tmp_path / "scripts").mkdir()
    assert cfl.main(["--root", str(tmp_path), "--strict"]) == 1
    assert "601 lines exceeds the 600-line cap" in capsys.readouterr().out


def test_strict_passes_on_clean_tree(tmp_path):
    _write(tmp_path, "backend/app/ok.py", 600)
    assert cfl.main(["--root", str(tmp_path), "--strict"]) == 0

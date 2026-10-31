"""Tests for the grandfather-list rule in scripts/policy/check_grandfather_lists.py (#820).

A PR may not add an exception to a gate (file-length allowlist, knip ignore*,
jscpd ignore) without an ``epic:refactor #NNN`` reference in the same file.
"""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "policy" / "check_grandfather_lists.py"

_spec = importlib.util.spec_from_file_location("check_grandfather_lists", SCRIPT)
policy = importlib.util.module_from_spec(_spec)
sys.modules["check_grandfather_lists"] = policy
_spec.loader.exec_module(policy)

ALLOW = "scripts/file_length_allowlist.txt"
KNIP = "frontend/knip.json"


def _diff(path: str, added=(), removed=(), start=1) -> str:
    body = [f"-{r}" for r in removed] + [f"+{a}" for a in added]
    return (
        f"--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,{len(removed)} +{start},{len(added)} @@\n" + "\n".join(body) + "\n"
    )


def _gf(diff: str, new_text: str | None = None):
    return policy.check_grandfather_diff(diff, lambda _p: new_text)


def _checks(violations) -> set[str]:
    return {v.check for v in violations}


def test_allowlist_new_entry_without_ref_fails():
    found = _gf(_diff(ALLOW, ["backend/app/big.py 700"]))
    assert _checks(found) == {"grandfather-list"}
    assert found[0].path == ALLOW


def test_allowlist_new_entry_with_epic_ref_passes():
    line = "backend/app/big.py 700  # epic:refactor #123 split planned"
    assert _gf(_diff(ALLOW, [line])) == []


def test_allowlist_comment_only_addition_passes():
    assert _gf(_diff(ALLOW, ["# just a note"])) == []


def test_allowlist_shrinking_a_cap_passes_but_growing_fails():
    shrink = _diff(ALLOW, ["a/b.py 650"], removed=["a/b.py 700"])
    assert _gf(shrink) == []
    grow = _diff(ALLOW, ["a/b.py 750"], removed=["a/b.py 700"])
    assert _checks(_gf(grow)) == {"grandfather-list"}


KNIP_TEXT = """{
  // reasons live here
  "ignoreDependencies": [
    "@babel/core",
    "new-dep"
  ],
  "entry": ["app/**/*.ts"]
}
"""


def test_knip_new_ignore_entry_without_ref_fails():
    diff = _diff(KNIP, ['    "new-dep"'], start=5)
    assert _checks(_gf(diff, KNIP_TEXT)) == {"grandfather-list"}


def test_knip_new_ignore_entry_with_epic_ref_passes():
    text = KNIP_TEXT.replace('    "new-dep"', '    // epic:refactor #99\n    "new-dep"')
    diff = _diff(KNIP, ["    // epic:refactor #99", '    "new-dep"'], start=5)
    assert _gf(diff, text) == []


def test_knip_change_outside_ignore_keys_passes():
    diff = _diff(KNIP, ['  "entry": ["app/**/*.ts"]'], start=7)
    assert _gf(diff, KNIP_TEXT) == []


def test_knip_new_ignore_key_with_entries_fails_but_empty_passes():
    text = '{\n  "ignoreIssues": {\n    "a.ts": ["exports"]\n  }\n}\n'
    diff = _diff(
        KNIP, ['  "ignoreIssues": {', '    "a.ts": ["exports"]', "  }"], start=2
    )
    assert _checks(_gf(diff, text)) == {"grandfather-list"}
    empty = '{\n  "ignore": [],\n  "entry": []\n}\n'
    assert _gf(_diff(KNIP, ['  "ignore": [],'], start=2), empty) == []


def test_jscpd_ignore_entry_needs_ref():
    text = '{\n  "ignore": [\n    "**/node_modules/**",\n    "**/new/**"\n  ]\n}\n'
    diff = _diff(".jscpd.json", ['    "**/new/**"'], start=4)
    assert _checks(_gf(diff, text)) == {"grandfather-list"}
    ref = _diff(
        ".jscpd.json",
        ['    "**/new/**"', '  "//": "epic:refactor #7 until split"'],
        start=4,
    )
    assert _gf(ref, text) == []


def test_reordered_ignore_entry_passes():
    diff = _diff(KNIP, ['    "new-dep"'], removed=['    "new-dep"'], start=5)
    assert _gf(diff, KNIP_TEXT) == []


def test_unwatched_files_are_ignored():
    assert _gf(_diff("docs/x.md", ["- ignore: foo"])) == []


def test_parse_diff_tracks_new_line_numbers():
    d = _diff(KNIP, ["a", "b"], start=10)
    assert policy.parse_diff(d)[KNIP][0] == {10: "a", 11: "b"}


def test_cli_diff_base_end_to_end(tmp_path):
    ident = ["-c", "user.email=t@t", "-c", "user.name=t"]

    def sh(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    (tmp_path / "scripts").mkdir()
    (tmp_path / "backend/app/ai").mkdir(parents=True)
    shutil.copy(
        REPO / "backend/app/ai/models_allowlist.py",
        tmp_path / "backend/app/ai/models_allowlist.py",
    )
    allow = tmp_path / ALLOW
    allow.write_text("# header\n")
    sh("init", "-q", "-b", "main")
    sh(*ident, "add", "-A")
    sh(*ident, "commit", "-qm", "base")
    allow.write_text("# header\nbackend/app/big.py 700\n")
    sh(*ident, "commit", "-qam", "add")
    assert policy.main(["--root", str(tmp_path), "--diff-base", "HEAD~1"]) == 1
    allow.write_text("# header\nbackend/app/big.py 700 # epic:refactor #5\n")
    sh(*ident, "commit", "-qam", "ref")
    assert policy.main(["--root", str(tmp_path), "--diff-base", "HEAD~2"]) == 0

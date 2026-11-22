"""Tests for scripts/policy/check_grandfather_lists.py (#820).

A PR may not add an exception to a gate, or loosen one, without an
``epic:refactor #NNN`` (or ``owner-approved #NNN``) line in the same file.
Detection compares the old and new collections, not diff lines.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "policy" / "check_grandfather_lists.py"

_spec = importlib.util.spec_from_file_location("check_grandfather_lists", SCRIPT)
policy = importlib.util.module_from_spec(_spec)
sys.modules["check_grandfather_lists"] = policy
_spec.loader.exec_module(policy)

ALLOW = "scripts/file_length_allowlist.txt"
KNIP = "frontend/knip.json"
JSCPD = ".jscpd.json"


def _check(path, old, new, added=()):
    return policy.check_changes({path: (old, new, list(added))})


def _bad(path, old, new, added=()):
    found = _check(path, old, new, added)
    assert [v.check for v in found] == ["grandfather-list"]
    assert found[0].path == path


KNIP_OLD = """{
  // reasons live here
  "entry": ["app/**/*.ts"],
  "ignoreDependencies": [
    "@babel/core",
    "react-test-renderer"
  ]
}
"""


# --- allowlist ---------------------------------------------------------------


def test_allowlist_new_entry_without_marker_fails():
    _bad(ALLOW, "# header\n", "# header\nbackend/app/big.py 700\n")


@pytest.mark.parametrize("marker", ["epic:refactor #123", "owner-approved #9"])
def test_allowlist_new_entry_with_marker_passes(marker):
    line = f"backend/app/big.py 700  # {marker}"
    assert _check(ALLOW, "", line + "\n", [line]) == []


def test_allowlist_comment_only_addition_passes():
    assert _check(ALLOW, "# a\n", "# a\n# note\n", ["# note"]) == []


def test_allowlist_lowering_removing_and_reordering_pass():
    old = "a.py 700\nb.py 800\n"
    assert _check(ALLOW, old, "a.py 650\nb.py 800\n") == []
    assert _check(ALLOW, old, "b.py 800\n") == []
    assert _check(ALLOW, old, "b.py 800\na.py 700\n") == []


def test_allowlist_raising_a_cap_fails():
    _bad(ALLOW, "a.py 700\n", "a.py 750\n")


# --- knip / ignore* collections -----------------------------------------------


def test_deleting_the_last_entry_is_not_a_new_exception():
    new = KNIP_OLD.replace('"@babel/core",\n    "react-test-renderer"', '"@babel/core"')
    # The diff line for "@babel/core" changed (trailing comma dropped): line-based
    # detection flagged it, collection-based detection must not.
    assert _check(KNIP, KNIP_OLD, new, ['    "@babel/core"']) == []


def test_reordering_entries_passes():
    new = KNIP_OLD.replace(
        '"@babel/core",\n    "react-test-renderer"',
        '"react-test-renderer",\n    "@babel/core"',
    )
    assert _check(KNIP, KNIP_OLD, new, ['    "react-test-renderer",']) == []


def test_appending_to_an_existing_array_fails():
    new = KNIP_OLD.replace(
        '"react-test-renderer"', '"react-test-renderer",\n    "new-dep"'
    )
    _bad(KNIP, KNIP_OLD, new)


def test_renaming_a_key_to_ignore_fails():
    new = KNIP_OLD.replace('"entry"', '"ignore"')
    _bad(KNIP, KNIP_OLD, new, ['  "ignore": ["app/**/*.ts"],'])


def test_new_entry_with_marker_in_jsonc_passes():
    new = KNIP_OLD.replace(
        '"react-test-renderer"',
        '"react-test-renderer",\n    // epic:refactor #99\n    "new-dep"',
    )
    assert (
        _check(KNIP, KNIP_OLD, new, ["    // epic:refactor #99", '    "new-dep"']) == []
    )


def test_nested_ignore_keys_are_found():
    old = '{"workspaces": {"a": {"ignore": ["x"]}}}'
    new = '{"workspaces": {"a": {"ignore": ["x", "y"]}}}'
    _bad(KNIP, old, new)
    _bad(KNIP, old, '{"workspaces": {"b": {"ignoreIssues": {"f.ts": ["exports"]}}}}')


def test_changes_outside_ignore_keys_pass():
    new = KNIP_OLD.replace('"app/**/*.ts"', '"app/**/*.ts", "lib/**/*.ts"')
    assert _check(KNIP, KNIP_OLD, new) == []


def test_empty_ignore_keys_pass_and_new_file_with_entries_fails():
    assert _check(KNIP, "{}", '{"ignore": [], "ignoreIssues": {}}') == []
    _bad(KNIP, None, '{"ignore": ["a.ts"]}')


def test_unparseable_config_fails_closed():
    _bad(KNIP, KNIP_OLD, "{ not json")


# --- guarded file set -----------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "knip.json",
        "knip.jsonc",
        ".knip.json",
        ".knip.jsonc",
        "frontend/knip.json",
        "frontend/.knip.jsonc",
        "frontend/package.json",
        ".jscpd.json",
        ".jscpd.tests.json",
        ALLOW,
        "frontend/knip.ts",
        "frontend/knip.config.ts",
        "frontend/knip.js",
        "frontend/knip.config.js",
    ],
)
def test_every_knip_and_jscpd_config_is_guarded(path):
    assert policy.is_guarded(path)


@pytest.mark.parametrize("path", ["docs/testing.md", "frontend/lib/knip.tsx", "a.json"])
def test_other_files_are_not_guarded(path):
    assert not policy.is_guarded(path)
    assert _check(path, "{}", '{"ignore": ["x"]}') == []


@pytest.mark.parametrize("path", ["frontend/knip.ts", "knip.config.js"])
def test_adding_a_knip_code_config_fails(path):
    _bad(path, None, "export default {};\n")
    assert _check(path, "export default {};\n", "export default {a: 1};\n") == []


def test_knip_key_in_package_json_is_guarded():
    pkg = "frontend/package.json"
    old = json.dumps({"name": "x", "knip": {"ignoreDependencies": ["a"]}})
    new = json.dumps({"name": "x", "knip": {"ignoreDependencies": ["a", "b"]}})
    _bad(pkg, old, new)
    other = json.dumps({"name": "y", "knip": {"ignoreDependencies": ["a"]}})
    assert _check(pkg, old, other) == []
    marked = json.dumps(
        {"//": "epic:refactor #1", "knip": {"ignoreDependencies": ["a", "b"]}}
    )
    assert _check(pkg, old, marked, ['"//": "epic:refactor #1"']) == []


# --- jscpd loosening ---------------------------------------------------------------


JS_OLD = '{"threshold": 0.1, "minLines": 5, "minTokens": 50, "ignore": ["a"]}'


@pytest.mark.parametrize(
    "field,value", [("threshold", 0.5), ("minLines", 6), ("minTokens", 60)]
)
def test_jscpd_loosening_fails(field, value):
    cfg = json.loads(JS_OLD)
    cfg[field] = value
    _bad(JSCPD, JS_OLD, json.dumps(cfg))
    cfg["//"] = "epic:refactor #7 reason"
    assert (
        _check(JSCPD, JS_OLD, json.dumps(cfg), ['"//": "epic:refactor #7 reason"'])
        == []
    )


def test_jscpd_tightening_passes_and_removing_threshold_fails():
    tight = '{"threshold": 0.05, "minLines": 4, "minTokens": 40, "ignore": ["a"]}'
    assert _check(JSCPD, JS_OLD, tight) == []
    _bad(JSCPD, JS_OLD, '{"minLines": 5, "minTokens": 50, "ignore": ["a"]}')


def test_jscpd_ignore_entry_needs_marker():
    _bad(JSCPD, JS_OLD, JS_OLD.replace('["a"]', '["a", "b"]'))


# --- parser + CLI -----------------------------------------------------------------


def test_parse_jsonc_handles_comments_urls_and_trailing_commas():
    text = '{\n  "u": "https://x.test/a", // note\n  /* block */ "a": [1, 2,],\n}'
    assert policy.parse_jsonc(text) == {"u": "https://x.test/a", "a": [1, 2]}


def test_parse_jsonc_keeps_commas_inside_strings():
    text = '{"ignore": ["**/*.{ts,tsx,}", "a,]", "b",],}'
    assert policy.parse_jsonc(text) == {"ignore": ["**/*.{ts,tsx,}", "a,]", "b"]}


def test_changing_a_comma_inside_an_ignore_glob_fails():
    old = '{"ignore": ["**/*.{ts,tsx}"]}'
    new = '{"ignore": ["**/*.{ts,tsx,}"]}'
    _bad(KNIP, old, new)


def test_real_repo_configs_parse():
    for rel in ("frontend/knip.json", ".jscpd.json", ".jscpd.tests.json"):
        policy.parse_jsonc((REPO / rel).read_text())


def test_cli_end_to_end(tmp_path):
    ident = ["-c", "user.email=t@t", "-c", "user.name=t"]

    def sh(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    (tmp_path / "scripts").mkdir()
    (tmp_path / "frontend").mkdir()
    allow = tmp_path / ALLOW
    knip = tmp_path / KNIP
    allow.write_text("# header\n")
    knip.write_text('{"ignoreDependencies": ["a", "b"]}\n')
    sh("init", "-q", "-b", "main")
    sh(*ident, "add", "-A")
    sh(*ident, "commit", "-qm", "base")
    sh("branch", "base")
    args = ["--root", str(tmp_path), "--diff-base", "base"]

    knip.write_text('{"ignoreDependencies": ["a"]}\n')  # removal: fine
    sh(*ident, "commit", "-qam", "remove")
    assert policy.main(args) == 0

    allow.write_text("# header\nbackend/app/big.py 700\n")
    sh(*ident, "commit", "-qam", "add")
    assert policy.main(args) == 1

    allow.write_text("# header\nbackend/app/big.py 700 # epic:refactor #5\n")
    sh(*ident, "commit", "-qam", "marker")
    assert policy.main(args) == 0

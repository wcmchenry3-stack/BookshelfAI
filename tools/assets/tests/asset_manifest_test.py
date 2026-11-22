"""Every shipped asset has a provenance entry and a recipe, or a stated reason it has none.

Reads tools/assets/asset_manifest.json. Fails when a file under frontend/assets/ matches no rule or
more than one, when a rule names a recipe script or credits file that does not exist, when a
third-party rule is not in the allow-list (so a third-party file cannot arrive unnoticed), or when an
allow-listed file is gone. Mirrors BC Arcade's check; see docs/ASSETS.md.
"""

import json
import re
from pathlib import Path

TOOLS_ASSETS = Path(__file__).resolve().parents[1]
REPO = TOOLS_ASSETS.parents[1]
MANIFEST = json.loads((TOOLS_ASSETS / "asset_manifest.json").read_text())
RULES = MANIFEST["rules"]
ALLOW = set(MANIFEST["third_party_allowlist"])
KINDS = {"generated", "attested", "third-party"}


def _shipped() -> list[str]:
    return sorted(
        p.relative_to(REPO).as_posix() for p in (REPO / "frontend/assets").rglob("*") if p.is_file()
    )


def _matches(rule: dict, path: str) -> bool:
    # "**" spans folders, "*" stays inside one.
    rx = re.escape(rule["glob"]).replace(r"\*\*/", "(?:.*/)?").replace(r"\*", "[^/]*")
    return re.fullmatch(rx, path) is not None


def _recipe_scripts(recipe: str) -> list[str]:
    return re.findall(r"tools/assets/(\w+\.py)", recipe)


def test_every_asset_matches_exactly_one_rule():
    bad = {}
    for path in _shipped():
        hits = [r["glob"] for r in RULES if _matches(r, path)]
        if len(hits) != 1:
            bad[path] = hits
    assert not bad, f"assets without exactly one manifest rule: {bad}"


def test_rules_are_well_formed_and_point_at_real_files():
    for rule in RULES:
        assert rule["kind"] in KINDS, rule
        if "credits" in rule:
            assert (REPO / rule["credits"]).is_file(), rule
        if rule["kind"] == "generated":
            assert rule.get("recipe"), f"{rule['glob']} is generated, so it needs a recipe"
        else:
            assert rule.get("why"), f"{rule['glob']} needs a why"
        for script in _recipe_scripts(rule.get("recipe", "")):
            assert (TOOLS_ASSETS / script).is_file(), (rule["glob"], script)


def test_third_party_files_are_allow_listed():
    third_party = {p for p in _shipped() for r in RULES if r["kind"] == "third-party" and _matches(r, p)}
    assert third_party <= ALLOW, f"third-party files not on the allow-list: {third_party - ALLOW}"
    for path in ALLOW:
        assert (REPO / path).is_file(), f"allow-listed file is gone: {path}"

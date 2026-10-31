"""Structural checks on ``app/api/scan.py`` after the #808 split, read from
the AST so a later edit cannot quietly undo them:

- every route keeps its ``@limiter.limit(settings.rate_limit_*)``;
- every name the suite replaces with ``patch("app.api.scan.<name>")`` is
  still a module attribute *and* looked up by name inside the route module,
  so the patch reaches running code;
- the helpers in ``app/services/scan/`` share the module objects the suite
  patches attributes on (``app.api.scan.ledger.record`` etc.);
- ``_reserve_scan_quota`` maps only the quota refusal classes (anything else
  fails closed as a 500);
- the provider call refunds in exactly two places and releases the lease in
  its ``finally``.
"""

import ast
import inspect
from pathlib import Path

import pytest

from app.api import scan as scan_api
from app.services.scan import quota_steps, records, settlement

_SOURCE = Path(scan_api.__file__).read_text()
_TREE = ast.parse(_SOURCE)
_FUNCS = {
    n.name: n
    for n in _TREE.body
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
}

# Names the existing suite replaces on app.api.scan by name (patch(...) /
# monkeypatch.setattr / patch.object(scan_api, ...)).
PATCHED_BY_NAME = [
    "_count_scan_request",
    "_reserve_scan_quota",
    "_refund_scan_quota",
    "_release_scan_lease",
    "_verify_turnstile",
    "_usage_row",
    "_settlement",
    "_dedupe_enriched",
    "_mark_library",
    "run_feature",
    "get_ai_settings",
    "get_feature",
    "sanitize_image",
    "settings",
    "ScanResponse",
    "EnrichmentService",
    "DeduplicationService",
]


def _loaded_names(node: ast.AST) -> set[str]:
    return {
        n.id
        for n in ast.walk(node)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(node)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == name
    ]


def test_every_route_keeps_its_rate_limit():
    routes = [
        f
        for f in _FUNCS.values()
        if any(
            isinstance(d, ast.Call)
            and isinstance(d.func, ast.Attribute)
            and isinstance(d.func.value, ast.Name)
            and d.func.value.id == "router"
            for d in f.decorator_list
        )
    ]
    assert {f.name for f in routes} == {"scan", "scan_usage"}
    for f in routes:
        limits = [
            d
            for d in f.decorator_list
            if isinstance(d, ast.Call)
            and ast.unparse(d.func) == "limiter.limit"
            and ast.unparse(d.args[0]).startswith("settings.rate_limit_")
        ]
        assert len(limits) == 1, f.name
    assert ast.unparse(_FUNCS["scan"].decorator_list[1].args[0]) == (
        "settings.rate_limit_scan"
    )
    assert ast.unparse(_FUNCS["scan_usage"].decorator_list[1].args[0]) == (
        "settings.rate_limit_reads"
    )


@pytest.mark.parametrize("name", PATCHED_BY_NAME)
def test_patched_names_are_looked_up_in_the_route_module(name):
    assert hasattr(scan_api, name)
    used = set()
    for f in _FUNCS.values():
        used |= _loaded_names(f)
    assert name in used, f"{name} is no longer looked up in app/api/scan.py"


def test_moved_helpers_share_the_patched_module_objects():
    assert records.scan_records is scan_api.scan_records
    assert settlement.ledger is scan_api.ledger
    assert quota_steps.quota is scan_api.quota
    assert records.quota is scan_api.quota


def test_reserve_maps_only_the_quota_refusals():
    tree = ast.parse(inspect.getsource(quota_steps.reserve_scan_quota))
    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
    caught = [ast.unparse(h.type) for h in handlers if h.type is not None]
    assert len(caught) == len(handlers), "bare except in reserve"
    assert caught == [
        "quota.EnhancedRequiresPremium",
        "quota.EnhancedThrottled",
        "quota.CostCeilingReached",
        "quota.GlobalCapacityReached",
        "quota.ScanInProgress",
        "quota.DailyLimitReached",
    ]


def test_provider_call_refunds_twice_and_releases_in_finally():
    run = _FUNCS["_run_provider"]
    assert len(_calls(run, "_refund_scan_quota")) == 2
    outer = [n for n in run.body if isinstance(n, ast.Try)]
    assert len(outer) == 1 and outer[0].finalbody
    released_in_finally = [
        c for stmt in outer[0].finalbody for c in _calls(stmt, "_release_scan_lease")
    ]
    assert len(released_in_finally) == 1
    assert len(_calls(run, "_release_scan_lease")) == 1


def test_scan_awaits_its_steps_in_order():
    steps = [
        "_load_scan_settings",
        "_count_scan_request",
        "_check_turnstile",
        "_read_validated_upload",
        "_normalise_image",
        "_fingerprint",
        "_serve_saved_answer",
        "_reserve_scan_quota",
        "_run_provider",
        "_record_and_enrich",
        "_enhanced_remaining",
    ]
    called = [
        n.func.id
        for n in ast.walk(_FUNCS["scan"])
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id in steps
    ]
    order = sorted(
        called,
        key=lambda name: min(c.lineno for c in _calls(_FUNCS["scan"], name)),
    )
    assert order == steps
    assert all(len(_calls(_FUNCS["scan"], s)) == 1 for s in steps)

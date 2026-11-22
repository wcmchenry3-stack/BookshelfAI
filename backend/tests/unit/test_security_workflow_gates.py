"""security.yml SCA gates must be able to fail and must see frontend changes (#857). Offline."""

from pathlib import Path

import yaml

WORKFLOW = (
    Path(__file__).resolve().parents[3] / ".github" / "workflows" / "security.yml"
)


def _load():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_pr_paths_include_frontend_manifests():
    paths = _load()[True]["pull_request"]["paths"]
    assert "frontend/package.json" in paths
    assert "frontend/package-lock.json" in paths


def test_npm_audit_job_fails_on_findings():
    steps = _load()["jobs"]["sca-npm-audit"]["steps"]
    runs = [s.get("run", "") for s in steps]
    assert any(
        r.strip().startswith("npm audit --audit-level=") and "--json" not in r
        for r in runs
    )
    uploads = [s for s in steps if "upload-artifact" in s.get("uses", "")]
    assert uploads and all(u.get("if") == "always()" for u in uploads)


def test_sca_jobs_do_not_swallow_failures():
    jobs = _load()["jobs"]
    for name in ("sca-npm-audit", "sca-pip-audit", "sca-trivy"):
        assert "continue-on-error" not in jobs[name]
        for s in jobs[name]["steps"]:
            assert "continue-on-error" not in s
            assert "|| true" not in s.get("run", "")

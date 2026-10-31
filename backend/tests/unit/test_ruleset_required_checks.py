"""The ruleset's required status checks must match real, always-reporting jobs (#771).

GitHub reports an in-repo job as its ``name:`` (else its job id) and a
reusable-workflow job as ``<caller job id> / <inner job name>``. A required
context that matches nothing, or whose workflow never runs on a PR (a
``paths`` filter), blocks every merge. Offline: reads files only.
"""

import json
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
RULESET = REPO_ROOT / ".github" / "rulesets" / "protect-main-dev.json"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
TARGET_BRANCH = "dev"
NATIVE_DIRECT = {"ios-build-check", "android-build-check"}


def load_required_contexts(path=RULESET):
    ruleset = json.loads(Path(path).read_text())
    for rule in ruleset["rules"]:
        if rule["type"] == "required_status_checks":
            return [c["context"] for c in rule["parameters"]["required_status_checks"]]
    return []


def load_workflows(directory=WORKFLOWS):
    return {
        p.name: yaml.safe_load(p.read_text())
        for p in sorted(Path(directory).glob("*.yml"))
    }


def _pull_request_trigger(workflow):
    """Return (triggers_on_pr, config dict). YAML parses ``on:`` as True."""
    on = workflow.get("on", workflow.get(True))
    if isinstance(on, str):
        on = [on]
    if isinstance(on, list):
        return "pull_request" in on, {}
    if isinstance(on, dict) and "pull_request" in on:
        return True, on["pull_request"] or {}
    return False, {}


def _workflow_problem(workflow):
    triggers, cfg = _pull_request_trigger(workflow)
    if not triggers:
        return "its workflow does not trigger on pull_request"
    if "paths" in cfg or "paths-ignore" in cfg:
        return "its workflow has a paths/paths-ignore filter on pull_request"
    branches = cfg.get("branches")
    if branches is not None and TARGET_BRANCH not in branches:
        return f"its workflow does not run for PRs into {TARGET_BRANCH}"
    return None


def _matches(context, job_id, job):
    if job.get("uses"):
        # Inner name of a remote reusable workflow is unreadable: accept any.
        # The plain id is what a skipped reusable job reports.
        return context.startswith(f"{job_id} / ")
    return context == (job.get("name") or job_id)


def check_required_contexts(contexts, workflows):
    """Return a list of problem strings (empty when the ruleset is sound)."""
    problems = []
    for context in contexts:
        if context in NATIVE_DIRECT or context.split(" / ")[0] in NATIVE_DIRECT:
            problems.append(
                f"{context!r}: never require native build jobs directly (a skipped "
                "run reports the plain id); require native-build-gate instead"
            )
            continue
        reasons = []
        found = False
        for wf_name, wf in workflows.items():
            for job_id, job in (wf.get("jobs") or {}).items():
                if not _matches(context, job_id, job):
                    continue
                found = True
                problem = _workflow_problem(wf)
                if problem is None:
                    break
                reasons.append(f"{wf_name}: {problem}")
            else:
                continue
            break
        else:
            if not found:
                problems.append(f"{context!r}: matches no workflow job")
            else:
                problems.append(f"{context!r}: " + "; ".join(reasons))
    return problems


def _wf(on, jobs):
    return {"on": on, "jobs": jobs}


class TestRulesetRequiredChecks:
    def test_ruleset_has_required_checks(self):
        assert load_required_contexts()

    def test_required_checks_match_real_always_reporting_jobs(self):
        problems = check_required_contexts(load_required_contexts(), load_workflows())
        assert not problems, "\n".join(problems)

    def test_native_build_gate_is_required(self):
        assert "native-build-gate" in load_required_contexts()


class TestCheckerRejectsBadRulesets:
    GOOD = _wf({"pull_request": {"branches": ["dev"]}}, {"lint": {"name": "Lint"}})

    def test_accepts_good_context(self):
        assert check_required_contexts(["Lint"], {"a.yml": self.GOOD}) == []

    def test_bogus_context_fails(self):
        problems = check_required_contexts(["No Such Job"], {"a.yml": self.GOOD})
        assert problems and "matches no workflow job" in problems[0]

    def test_path_filtered_job_fails(self):
        wf = _wf(
            {"pull_request": {"branches": ["dev"], "paths": ["frontend/**"]}},
            {"lint": {"name": "Lint"}},
        )
        problems = check_required_contexts(["Lint"], {"a.yml": wf})
        assert problems and "paths" in problems[0]

    def test_non_pr_workflow_fails(self):
        wf = _wf({"schedule": [{"cron": "0 0 * * *"}]}, {"lint": {}})
        assert check_required_contexts(["lint"], {"a.yml": wf})

    def test_other_branch_only_fails(self):
        wf = _wf({"pull_request": {"branches": ["main"]}}, {"lint": {}})
        assert check_required_contexts(["lint"], {"a.yml": wf})

    def test_reusable_job_accepts_any_inner_name(self):
        wf = _wf(
            {"pull_request": None},
            {"scan": {"uses": "o/r/.github/workflows/x.yml@main"}},
        )
        assert check_required_contexts(["scan / Whatever"], {"a.yml": wf}) == []
        assert check_required_contexts(["other / Whatever"], {"a.yml": wf})

    def test_native_build_jobs_rejected_with_pointer(self):
        wf = _wf(
            {"pull_request": None},
            {"ios-build-check": {"uses": "o/r/.github/workflows/x.yml@main"}},
        )
        for ctx in ("ios-build-check", "ios-build-check / Xcode Build (no signing)"):
            problems = check_required_contexts([ctx], {"a.yml": wf})
            assert problems and "native-build-gate" in problems[0]

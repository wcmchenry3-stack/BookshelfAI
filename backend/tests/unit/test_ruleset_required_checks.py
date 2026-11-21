"""The ruleset's required status checks must match real, always-reporting jobs (#771).

GitHub reports an in-repo job as its ``name:`` (else its job id) and a
reusable-workflow job as ``<caller job id> / <inner job name>``. A required
context that matches nothing, or whose workflow never runs on a PR (a
``paths`` filter), blocks every merge. Offline: reads files only.
"""

import json
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
RULESETS_DIR = REPO_ROOT / ".github" / "rulesets"
RULESET = RULESETS_DIR / "protect-dev.json"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
TARGET_BRANCH = "dev"
# (ruleset file, branch it protects): each file is validated against its own branch.
RULESET_FILES = [("protect-dev.json", "dev"), ("protect-main.json", "main")]
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


def _workflow_problem(workflow, target_branch=None):
    target_branch = target_branch or TARGET_BRANCH
    triggers, cfg = _pull_request_trigger(workflow)
    if not triggers:
        return "its workflow does not trigger on pull_request"
    if "paths" in cfg or "paths-ignore" in cfg:
        return "its workflow has a paths/paths-ignore filter on pull_request"
    branches = cfg.get("branches")
    if branches is not None and target_branch not in branches:
        return f"its workflow does not run for PRs into {target_branch}"
    return None


def _matches(context, job_id, job):
    if job.get("uses"):
        # Inner name of a remote reusable workflow is unreadable: accept any.
        # The plain id is what a skipped reusable job reports.
        return context.startswith(f"{job_id} / ")
    return context == (job.get("name") or job_id)


def check_required_contexts(contexts, workflows, target_branch=None):
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
                problem = _workflow_problem(wf, target_branch)
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


def _merge_group_trigger(workflow):
    """Return (triggers_on_merge_group, config dict)."""
    on = workflow.get("on", workflow.get(True))
    if isinstance(on, str):
        on = [on]
    if isinstance(on, list):
        return "merge_group" in on, {}
    if isinstance(on, dict) and "merge_group" in on:
        return True, on["merge_group"] or {}
    return False, {}


def workflows_supplying(contexts, workflows):
    """Names of the workflows that define a job reporting one of the contexts."""
    return {
        wf_name
        for context in contexts
        for wf_name, wf in workflows.items()
        for job_id, job in (wf.get("jobs") or {}).items()
        if _matches(context, job_id, job)
    }


def check_merge_group(contexts, workflows):
    """Problems for workflows that supply a context but never run in the merge queue.

    A required context that never reports on a queue entry stalls the queue.
    """
    problems = []
    for wf_name in sorted(workflows_supplying(contexts, workflows)):
        triggers, cfg = _merge_group_trigger(workflows[wf_name])
        if not triggers:
            problems.append(
                f"{wf_name}: supplies a required context but has no merge_group trigger"
            )
        elif cfg.get("types") is not None and "checks_requested" not in cfg["types"]:
            problems.append(
                f"{wf_name}: merge_group trigger does not include checks_requested"
            )
    return problems


def _wf(on, jobs):
    return {"on": on, "jobs": jobs}


@pytest.mark.parametrize("filename,branch", RULESET_FILES)
class TestRulesetRequiredChecks:
    def test_ruleset_has_required_checks(self, filename, branch):
        assert load_required_contexts(RULESETS_DIR / filename)

    def test_required_checks_match_real_always_reporting_jobs(self, filename, branch):
        problems = check_required_contexts(
            load_required_contexts(RULESETS_DIR / filename), load_workflows(), branch
        )
        assert not problems, "\n".join(problems)

    def test_native_build_gate_is_required(self, filename, branch):
        assert "native-build-gate" in load_required_contexts(RULESETS_DIR / filename)


class TestRulesetsAgree:
    def test_dev_and_main_list_identical_required_contexts(self):
        dev = load_required_contexts(RULESETS_DIR / "protect-dev.json")
        main = load_required_contexts(RULESETS_DIR / "protect-main.json")
        assert sorted(dev) == sorted(main)
        assert len(dev) == len(set(dev))

    def test_dev_required_workflows_run_in_merge_queue(self):
        problems = check_merge_group(load_required_contexts(), load_workflows())
        assert not problems, "\n".join(problems)


class TestMergeGroupCheck:
    def test_missing_merge_group_fails(self):
        wf = _wf({"pull_request": None}, {"lint": {"name": "Lint"}})
        problems = check_merge_group(["Lint"], {"a.yml": wf})
        assert problems and "merge_group" in problems[0]

    def test_merge_group_present_passes(self):
        wf = _wf(
            {"pull_request": None, "merge_group": {"types": ["checks_requested"]}},
            {"lint": {"name": "Lint"}},
        )
        assert check_merge_group(["Lint"], {"a.yml": wf}) == []

    def test_merge_group_without_checks_requested_fails(self):
        wf = _wf(
            {"pull_request": None, "merge_group": {"types": ["destroyed"]}},
            {"lint": {"name": "Lint"}},
        )
        assert check_merge_group(["Lint"], {"a.yml": wf})

    def test_unrelated_workflow_ignored(self):
        wf = _wf({"pull_request": None}, {"other": {"name": "Other"}})
        assert check_merge_group(["Lint"], {"a.yml": wf}) == []


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

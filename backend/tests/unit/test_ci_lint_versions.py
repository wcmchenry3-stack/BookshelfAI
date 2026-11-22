"""lint-python takes its black and ruff versions from backend/requirements-dev.txt (#1012).

Before this, ci.yml hard-coded ruff 0.17.1 while requirements-dev.txt pinned
ruff 0.17.6, and Dependabot bumped only the requirements file. The
``lint-versions`` job now reads both pins, and ``lint-python`` takes them as
inputs. Offline: reads ci.yml and runs the pin-reading script locally.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
REQUIREMENTS_DEV = REPO_ROOT / "backend" / "requirements-dev.txt"
RULESETS_DIR = REPO_ROOT / ".github" / "rulesets"


def _jobs():
    return yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _pin_script():
    """The run script of lint-versions' pin step (the one with id 'pins')."""
    steps = _jobs()["lint-versions"]["steps"]
    (step,) = [s for s in steps if s.get("id") == "pins"]
    return step["run"]


def _run_pin_script(tmp_path, requirements_text):
    (tmp_path / "requirements-dev.txt").write_text(requirements_text, encoding="utf-8")
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    env = {**os.environ, "GITHUB_OUTPUT": str(output)}
    result = subprocess.run(
        ["bash", "-c", _pin_script()],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, output.read_text(encoding="utf-8")


class TestLintPythonVersionInputs:
    def test_lint_python_needs_lint_versions(self):
        assert _jobs()["lint-python"]["needs"] == "lint-versions"

    @pytest.mark.parametrize(
        ("input_name", "output_name"),
        [("ruff-version", "ruff"), ("black-version", "black")],
    )
    def test_version_input_is_taken_from_lint_versions(self, input_name, output_name):
        value = _jobs()["lint-python"]["with"][input_name]
        assert value == f"${{{{ needs.lint-versions.outputs.{output_name} }}}}"

    def test_lint_python_keeps_its_job_id(self):
        # The ruleset requires "lint-python / Lint Python (black + ruff)".
        assert "lint-python" in _jobs()

    def test_lint_python_has_no_if_that_could_skip_it(self):
        assert "if" not in _jobs()["lint-python"]


class TestLintVersionsJob:
    def test_job_exists_with_read_only_permissions(self):
        job = _jobs()["lint-versions"]
        assert job["permissions"] == {"contents": "read"}
        assert job["runs-on"] == "ubuntu-latest"

    def test_job_runs_on_every_event_lint_python_does(self):
        # lint-python has no if:, so lint-versions must not either, or lint-python
        # would be skipped (and so pass its required check) on that event.
        assert "if" not in _jobs()["lint-versions"]

    def test_only_checkout_action_is_used(self):
        uses = [s["uses"] for s in _jobs()["lint-versions"]["steps"] if "uses" in s]
        assert uses == ["actions/checkout@v7"]

    def test_outputs_come_from_the_pin_step(self):
        outputs = _jobs()["lint-versions"]["outputs"]
        assert outputs == {
            "ruff": "${{ steps.pins.outputs.ruff }}",
            "black": "${{ steps.pins.outputs.black }}",
        }

    def test_job_is_a_required_context_in_both_rulesets(self):
        # A skipped lint-python passes its required check, so a failure here must
        # block the merge directly. The name is the context string GitHub reports.
        name = _jobs()["lint-versions"]["name"]
        assert name == "Lint versions (requirements-dev.txt)"
        for ruleset in ("protect-dev.json", "protect-main.json"):
            data = json.loads((RULESETS_DIR / ruleset).read_text(encoding="utf-8"))
            contexts = [
                c["context"]
                for rule in data["rules"]
                if rule["type"] == "required_status_checks"
                for c in rule["parameters"]["required_status_checks"]
            ]
            assert name in contexts, ruleset


class TestPinScript:
    def test_reads_pins_from_the_real_requirements_file(self, tmp_path):
        result, output = _run_pin_script(tmp_path, REQUIREMENTS_DEV.read_text())
        assert result.returncode == 0, result.stderr
        values = dict(line.split("=", 1) for line in output.splitlines())
        pins = {}
        for line in REQUIREMENTS_DEV.read_text().splitlines():
            name, sep, version = line.partition("==")
            if sep and name in ("ruff", "black"):
                pins[name] = version
        assert values == pins
        assert set(values) == {"ruff", "black"}

    def test_fails_when_ruff_pin_is_missing(self, tmp_path):
        text = "black==26.10.1\n"
        result, output = _run_pin_script(tmp_path, text)
        assert result.returncode != 0
        assert "ruff" in result.stdout + result.stderr
        assert output == ""

    def test_fails_when_black_pin_is_missing(self, tmp_path):
        text = "ruff==0.17.6\n"
        result, _output = _run_pin_script(tmp_path, text)
        assert result.returncode != 0
        assert "black" in result.stdout + result.stderr

    def test_fails_when_pin_is_duplicated(self, tmp_path):
        text = "ruff==0.17.6\nruff==0.17.5\nblack==26.10.1\n"
        result, _ = _run_pin_script(tmp_path, text)
        assert result.returncode != 0

    def test_fails_when_pin_is_not_an_exact_version(self, tmp_path):
        text = "ruff>=0.17\nblack==26.10.1\n"
        result, _ = _run_pin_script(tmp_path, text)
        assert result.returncode != 0

    def test_ignores_trailing_comment(self, tmp_path):
        text = "ruff==0.17.6  # linter\nblack==26.10.1\n"
        result, output = _run_pin_script(tmp_path, text)
        assert result.returncode == 0, result.stderr
        assert "ruff=0.17.6\n" in output

    def test_accepts_crlf_line_endings(self, tmp_path):
        text = "ruff==0.17.6\r\nblack==26.10.1\r\n"
        result, output = _run_pin_script(tmp_path, text)
        assert result.returncode == 0, result.stderr
        assert output.splitlines() == ["ruff=0.17.6", "black=26.10.1"]

    def test_accepts_env_marker_after_a_space(self, tmp_path):
        text = 'ruff==0.17.6 ; python_version >= "3.9"\nblack==26.10.1\n'
        result, output = _run_pin_script(tmp_path, text)
        assert result.returncode == 0, result.stderr
        assert output.splitlines() == ["ruff=0.17.6", "black=26.10.1"]

    def test_env_marker_without_a_space_fails_cleanly(self, tmp_path):
        # The version runs up to whitespace, so ';' stays in it and the plain-version
        # check rejects it. The job fails with a message and writes no outputs.
        text = 'ruff==0.17.6;python_version>="3.9"\nblack==26.10.1\n'
        result, output = _run_pin_script(tmp_path, text)
        assert result.returncode != 0
        assert "not a plain version" in result.stdout
        assert output == ""

"""No scan evaluation dataset file is ever tracked in git (#536).

The labelled photos and their ground truth live only in the private R2
bucket. This fails if an image or a manifest is tracked under
``backend/eval/``, or if ``.gitignore`` stops ignoring them."""

import shutil
import subprocess  # nosec B404 - fixed git commands, no shell
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
IMAGE_SUFFIXES = (
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".heic",
    ".heif",
    ".gif",
    ".tif",
    ".tiff",
    ".bmp",
    ".avif",
)
DATASET_NAMES = ("manifest.json", "ground_truth.jsonl")


def _git(*args: str) -> subprocess.CompletedProcess:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    return subprocess.run(  # nosec B603 - fixed argv
        [git, "-C", str(REPO), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


@pytest.fixture(scope="module")
def in_git_checkout():
    probe = _git("rev-parse", "--is-inside-work-tree")
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        pytest.skip("not a git checkout")


def dataset_files(paths: list[str]) -> list[str]:
    return [
        p
        for p in paths
        if p.startswith("backend/eval/")
        and (
            p.lower().endswith(IMAGE_SUFFIXES) or Path(p).name.lower() in DATASET_NAMES
        )
    ]


def test_dataset_file_filter():
    assert dataset_files(
        [
            "backend/eval/ai_scan/x.JPG",
            "backend/eval/data/manifest.json",
            "backend/eval/ai_scan/runner.py",
            "backend/eval/README.md",
            "frontend/assets/icon.png",
        ]
    ) == ["backend/eval/ai_scan/x.JPG", "backend/eval/data/manifest.json"]


def test_no_dataset_file_is_tracked_under_backend_eval(in_git_checkout):
    listed = _git("ls-files", "-z", "--", "backend/eval")
    assert listed.returncode == 0
    tracked = [p for p in listed.stdout.split("\0") if p]
    assert (
        dataset_files(tracked) == []
    ), "dataset files must live only in the private R2 bucket"


@pytest.mark.parametrize(
    "path",
    [
        "backend/eval/ai_scan/photo.jpg",
        "backend/eval/data/shelf.HEIC",
        "backend/eval/x/y/z.png",
        "backend/eval/data/manifest.json",
        "backend/ai-eval-report.json",
    ],
)
def test_gitignore_covers_dataset_files(in_git_checkout, path):
    result = _git("check-ignore", "--no-index", "-q", path)
    assert result.returncode == 0, f"{path} is not ignored"

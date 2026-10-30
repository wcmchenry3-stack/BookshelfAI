"""Tests for the Claude Code PreToolUse hooks under ``.claude/hooks/`` (#772).

Each hook is fed sample tool-input JSON on stdin, run from a throwaway git
repo (so branch checks are real), and judged by exit code: 0 = allow,
2 = block (reason on stderr).
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
HOOKS = REPO / ".claude" / "hooks"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None
    or shutil.which("bash") is None
    or shutil.which("python3") is None,
    reason="Claude hooks need jq, bash and python3",
)

GIT = [
    "git",
    "-c",
    "user.email=t@t",
    "-c",
    "user.name=t",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.hooksPath=/dev/null",
]


def _git(cwd, *args):
    subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """Temp git repo on a feature branch with one commit."""
    _git(tmp_path, "-c", "init.defaultBranch=main", "init", "-q")
    (tmp_path / "README.txt").write_text("base\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "chore: base")
    _git(tmp_path, "checkout", "-q", "-b", "feature/x")
    return tmp_path


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))


def run_hook(name, payload, cwd):
    return subprocess.run(
        ["bash", str(HOOKS / name)],
        cwd=cwd,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )


def bash_payload(command):
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def assert_allow(result):
    assert result.returncode == 0, result.stderr


def assert_block(result, contains=""):
    assert result.returncode == 2, (result.returncode, result.stderr)
    assert result.stderr.startswith("BLOCKED:")
    assert contains in result.stderr


# ── commit-gate.sh ────────────────────────────────────────────────────────


class TestCommitGate:
    def gate(self, repo, command):
        return run_hook("commit-gate.sh", bash_payload(command), repo)

    @pytest.mark.parametrize(
        "command",
        [
            'git commit -m "feat(app): add thing"',
            "git commit -m 'fix: handle null'",
            'git commit -m "feat: add thing" -m "Body paragraph."',
            'git commit -am "chore(deps): bump"',
            'git commit --allow-empty "-amfix: ok"',
            'git commit -sm "fix: signed off"',
            'git commit -m"docs: readme"',
            'git commit --message="test: cover x"',
            'git add -A && git commit -m "refactor(api): split router"',
            'git -C . commit -m "perf: faster"',
            "git commit --amend --no-edit",
            "git commit",
            "git commit -C HEAD",
            "git commit --fixup HEAD~1",
            'git commit -m "$(git log -1 --format=%s)"',
            "git commit -m \"Merge branch 'dev' into feature/x\"",
            "git status",
            'echo "git commit is fun"',
        ],
    )
    def test_allows(self, repo, command):
        assert_allow(self.gate(repo, command))

    @pytest.mark.parametrize(
        "command",
        [
            'git commit -m "added a thing"',
            'git commit -m "Feat: capital type"',
            'git commit -m "feature: wrong type"',
            'git commit -m "feat: Add thing"',
            'git commit -m "feat: ends with period."',
            'git commit -m "feat:"',
            'git commit -m "feat(Scope): upper scope"',
            'git commit -m "' + "feat: " + "x" * 100 + '"',
            'git commit -m "bad header" -m "feat: fine second paragraph"',
            'git add . && git commit -am "wip"',
            # Codex round 17: message attached inside a short-option cluster.
            "git commit --allow-empty -aminvalid",
            "git commit -avminvalid",
            'git commit -sm "bad header"',
        ],
    )
    def test_blocks_bad_message(self, repo, command):
        assert_block(self.gate(repo, command), "Conventional Commit")

    def test_header_exactly_max_length_allowed(self, repo):
        header = "feat: " + "x" * 94  # 100 chars
        assert len(header) == 100
        assert_allow(self.gate(repo, f'git commit -m "{header}"'))

    def test_blocks_heredoc_bad(self, repo):
        cmd = "git commit -m \"$(cat <<'EOF'\nupdated stuff\n\nlonger body\nEOF\n)\""
        assert_block(self.gate(repo, cmd), "Conventional Commit")

    def test_allows_heredoc_good(self, repo):
        cmd = (
            "git commit -m \"$(cat <<'EOF'\nfeat(hooks): add gate\n\n"
            'Body with "quotes".\n\nCo-Authored-By: X <x@y>\nEOF\n)"'
        )
        assert_allow(self.gate(repo, cmd))

    def test_file_form(self, repo):
        (repo / "msg.txt").write_text("bad message\n")
        assert_block(self.gate(repo, "git commit -F msg.txt"), "Conventional Commit")
        (repo / "msg.txt").write_text("# comment\nfix(api): good one\n\nbody\n")
        assert_allow(self.gate(repo, "git commit -F msg.txt"))
        assert_allow(self.gate(repo, "git commit --file=msg.txt"))
        assert_allow(self.gate(repo, "git commit -F missing.txt"))

    def test_stdin_heredoc_form(self, repo):
        bad = "git commit -F - <<'EOF'\nnope\nEOF\n"
        good = "git commit -F - <<'EOF'\nfix: ok\nEOF\n"
        assert_block(self.gate(repo, bad), "Conventional Commit")
        assert_allow(self.gate(repo, good))

    @pytest.mark.parametrize("branch", ["dev", "main"])
    def test_blocks_protected_branch(self, repo, branch):
        _git(repo, "checkout", "-q", "-B", branch)
        result = self.gate(repo, 'git commit -m "feat: fine message"')
        assert_block(result, branch)

    def test_branch_check_ignores_non_commit(self, repo):
        _git(repo, "checkout", "-q", "-B", "dev")
        assert_allow(self.gate(repo, "git status"))

    def test_empty_input_allows(self, repo):
        result = subprocess.run(
            ["bash", str(HOOKS / "commit-gate.sh")],
            cwd=repo,
            input="",
            capture_output=True,
            text=True,
            check=False,
        )
        assert_allow(result)


# ── pr-title-gate.sh ──────────────────────────────────────────────────────


class TestPrTitleGate:
    def gate(self, repo, command):
        return run_hook("pr-title-gate.sh", bash_payload(command), repo)

    def mcp(self, repo, **tool_input):
        payload = {
            "tool_name": "mcp__github__create_pull_request",
            "tool_input": tool_input,
        }
        return run_hook("pr-title-gate.sh", payload, repo)

    @pytest.mark.parametrize(
        "command",
        [
            'gh pr create --base dev --title "feat(app): add thing" --body "x"',
            "gh pr create -B dev -t 'fix: bug' --draft",
            'gh pr create --title="chore: x" --base=dev',
            "gh pr create --fill --base dev",
            "gh pr create -Bdev -t'fix: ok'",
            'gh pr new --base dev --title "fix: ok"',
            'gh pr create --base dev --title "$(git log -1 --format=%s)"',
            'gh pr create --base main --head dev --title "chore: release"',
            'gh pr create --base main --head release-please--branches--main --title "chore: release 1.2.3"',
            "gh pr list --base main",
            "git status",
        ],
    )
    def test_allows(self, repo, command):
        assert_allow(self.gate(repo, command))

    @pytest.mark.parametrize(
        "command, needle",
        [
            ('gh pr create --base dev --title "Added a thing"', "title"),
            ('gh pr create --base dev --title "Feat: x"', "title"),
            ('gh pr create --base dev --title "feat: Add thing."', "title"),
            ("gh pr create -t 'wip' --base dev", "title"),
            ('gh pr create --base main --title "feat: x"', "base 'main'"),
            (
                'gh pr create --base main --head feature/x --title "feat: x"',
                "base 'main'",
            ),
            ('gh pr create --base staging --title "feat: x"', "base 'staging'"),
            ('gh pr create -B develop -t "feat: x"', "base 'develop'"),
            # Codex round 9: gh would use gh-merge-base config or the repo default.
            ('gh pr create --title "feat: x"', "pass the PR base explicitly"),
            ("gh pr create --fill", "pass the PR base explicitly"),
            # Codex round 11: attached short values and the `new` alias.
            ("gh pr create -tinvalid --base dev --head feature/x", "title"),
            ('gh pr create -Bmain -t "feat: x" --head feature/x', "base 'main'"),
            ("gh pr new --title invalid --base main --head feature/x", "title"),
            ('gh -R o/r pr new --base staging --title "fix: x"', "base 'staging'"),
        ],
    )
    def test_blocks(self, repo, command, needle):
        assert_block(self.gate(repo, command), needle)

    @pytest.mark.parametrize(
        "cmd",
        [
            'gh pr create --base main --title "chore: release"',
            # Codex round 14: an earlier cd changes gh's implicit head.
            'cd ../feature-wt && gh pr create --base main --title "fix: x"',
        ],
    )
    def test_main_base_requires_explicit_head(self, repo, cmd):
        _git(repo, "checkout", "-q", "-B", "dev")
        assert_block(self.gate(repo, cmd), "must name its head explicitly")

    def test_mcp_allows(self, repo):
        assert_allow(
            self.mcp(repo, title="feat(app): add thing", base="dev", head="feature/x")
        )
        assert_allow(self.mcp(repo, title="chore: release", base="main", head="dev"))
        assert_allow(
            self.mcp(
                repo,
                title="chore: release",
                base="main",
                head="release-please--branches--main",
            )
        )

    def test_mcp_blocks(self, repo):
        assert_block(self.mcp(repo, title="Added thing", base="dev", head="f"), "title")
        assert_block(
            self.mcp(repo, title="feat: x", base="main", head="feature/x"),
            "base 'main'",
        )
        assert_block(
            self.mcp(repo, title="feat: x", base="staging", head="f"), "base 'staging'"
        )
        assert_block(
            self.mcp(repo, title="feat: x", base="main", head="fork:feature/x"), "base"
        )

    def test_mcp_head_with_owner_prefix(self, repo):
        assert_allow(self.mcp(repo, title="chore: x", base="main", head="owner:dev"))


# ── precheck-gate.sh ──────────────────────────────────────────────────────


@pytest.mark.skipif(
    shutil.which("ruff") is None or shutil.which("black") is None,
    reason="precheck backend tests need ruff and black",
)
class TestPrecheckGate:
    @pytest.fixture
    def pre_repo(self, repo):
        """Repo with an origin/dev ref at the base commit."""
        _git(repo, "update-ref", "refs/remotes/origin/dev", "HEAD")
        (repo / "backend").mkdir()
        return repo

    def commit_py(self, repo, content):
        (repo / "backend" / "mod.py").write_text(content)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "feat: change")

    def gate(self, repo, command="git push -u origin feature/x"):
        return run_hook("precheck-gate.sh", bash_payload(command), repo)

    def test_passes_clean_python_and_caches(self, pre_repo, tmp_path):
        self.commit_py(pre_repo, "X = 1\n")
        assert_allow(self.gate(pre_repo))
        cache = tmp_path / "xdg-cache" / "bookshelf-claude-hooks"
        tree = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"],
            cwd=pre_repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert (cache / f"precheck-{tree}").exists()

    def test_blocks_unformatted_python(self, pre_repo):
        self.commit_py(pre_repo, "x   =   1\nimport os\n")
        assert_block(self.gate(pre_repo), "Pre-push checks failed")

    def test_cached_pass_skips_checks(self, pre_repo):
        self.commit_py(pre_repo, "X = 1\n")
        assert_allow(self.gate(pre_repo))
        # Same tree hash: a (now bad) working copy is irrelevant, cache wins.
        (pre_repo / "backend" / "mod.py").write_text("x   =   1\n")
        assert_allow(self.gate(pre_repo))

    def test_failure_is_not_cached(self, pre_repo):
        self.commit_py(pre_repo, "x   =   1\n")
        assert_block(self.gate(pre_repo))
        assert_block(self.gate(pre_repo))

    def test_non_push_and_delete_allowed(self, pre_repo):
        self.commit_py(pre_repo, "x   =   1\n")
        assert_allow(self.gate(pre_repo, "git status"))
        assert_allow(self.gate(pre_repo, "git push origin --delete feature/old"))

    def test_non_ascii_python_filename_is_checked(self, pre_repo):
        # Codex round 13: git quotes non-ASCII names unless -z is used.
        (pre_repo / "backend" / "módulo.py").write_text("x   =   1\nimport os\n")
        _git(pre_repo, "add", "-A")
        _git(pre_repo, "commit", "-qm", "feat: change")
        assert_block(self.gate(pre_repo), "Pre-push checks failed")

    def test_no_python_changes_allows(self, pre_repo):
        (pre_repo / "notes.txt").write_text("hi\n")
        _git(pre_repo, "add", "-A")
        _git(pre_repo, "commit", "-qm", "docs: notes")
        assert_allow(self.gate(pre_repo))


class TestPrecheckGateNoTools:
    def test_missing_origin_dev_allows_with_warning(self, repo):
        result = run_hook("precheck-gate.sh", bash_payload("git push"), repo)
        assert_allow(result)
        assert "origin/dev" in result.stderr

    def test_missing_tools_warns_and_allows(self, repo, tmp_path):
        _git(repo, "update-ref", "refs/remotes/origin/dev", "HEAD")
        (repo / "backend").mkdir()
        (repo / "backend" / "m.py").write_text("x   =   1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "feat: m")
        # PATH with only the basics, no ruff/black.
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        for tool in (
            "bash",
            "git",
            "jq",
            "python3",
            "cat",
            "date",
            "dirname",
            "tr",
            "mkdir",
            "stat",
            "head",
            "sed",
            "rm",
            "env",
            "timeout",
            "uname",
            "readlink",
        ):
            found = shutil.which(tool)
            if found:
                (bin_dir / tool).symlink_to(found)
        env = {
            "PATH": str(bin_dir),
            "XDG_CACHE_HOME": str(tmp_path / "xdg"),
            "HOME": str(tmp_path),
        }
        result = subprocess.run(
            [str(bin_dir / "bash"), str(HOOKS / "precheck-gate.sh")],
            cwd=repo,
            input=json.dumps(bash_payload("git push")),
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert_allow(result)
        assert "not installed" in result.stderr


# ── lint-gate.sh / lib helpers ────────────────────────────────────────────


def test_lint_gate_ignores_non_pr_commands(repo):
    assert_allow(run_hook("lint-gate.sh", bash_payload("git status"), repo))


def test_cache_lib_ttl(tmp_path):
    script = f"""
    source {HOOKS}/lib/cache.sh
    cache_fresh k 60 && echo bad-initial
    cache_set k
    cache_fresh k 60 && echo fresh
    cache_fresh k 0 || echo expired
    cache_set '../escape'
    ls -A "$(cache_dir)"
    """
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "XDG_CACHE_HOME": str(tmp_path)},
        check=False,
    )
    assert "bad-initial" not in result.stdout
    assert "fresh" in result.stdout
    assert "expired" in result.stdout
    assert ".._escape" in result.stdout
    assert not (tmp_path.parent / "escape").exists()


def test_settings_registers_hooks():
    settings = json.loads((REPO / ".claude" / "settings.json").read_text())
    pre = settings["hooks"]["PreToolUse"]
    bash_cmds = [
        h["command"] for e in pre if e["matcher"] == "Bash" for h in e["hooks"]
    ]
    for name in (
        "lint-gate",
        "policy-gate",
        "commit-gate",
        "pr-title-gate",
        "precheck-gate",
    ):
        assert f"bash .claude/hooks/{name}.sh" in bash_cmds
    mcp = [e for e in pre if e["matcher"] == "mcp__github__create_pull_request"]
    assert (
        mcp and mcp[0]["hooks"][0]["command"] == "bash .claude/hooks/pr-title-gate.sh"
    )
    for path in settings["hooks"]["PreToolUse"]:
        for h in path["hooks"]:
            script = h["command"].split()[-1]
            assert (REPO / script).exists()


# ── Review regressions (PR #794) ──────────────────────────────────────────


def _init_repo(path, branch):
    path.mkdir(exist_ok=True)
    _git(path, "-c", "init.defaultBranch=" + branch, "init", "-q")
    (path / "f.txt").write_text("x\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "chore: base")


class TestGitDashC:
    """`git -C <dir>` selects the repo whose branch is checked."""

    def test_commit_in_feature_worktree_while_session_on_dev(self, repo):
        _git(repo, "checkout", "-q", "-B", "dev")
        _init_repo(repo / "wt", "feature-wt")
        cmd = 'git -C wt commit -m "feat: ok"'
        assert_allow(run_hook("commit-gate.sh", bash_payload(cmd), repo))

    def test_commit_in_protected_worktree_while_session_on_feature(self, repo):
        _init_repo(repo / "wt", "main")
        cmd = 'git -C wt commit -m "feat: ok"'
        assert_block(run_hook("commit-gate.sh", bash_payload(cmd), repo), "'main'")

    def test_repeated_c_composes_relatively(self, repo):
        _init_repo(repo / "wt", "dev")
        cmd = 'git -C . -C wt commit -m "feat: ok"'
        assert_block(run_hook("commit-gate.sh", bash_payload(cmd), repo), "'dev'")

    def test_message_file_resolved_against_c_dir(self, repo):
        _init_repo(repo / "wt", "feature-wt")
        (repo / "wt" / "msg.txt").write_text("not conventional\n")
        cmd = "git -C wt commit -F msg.txt"
        assert_block(
            run_hook("commit-gate.sh", bash_payload(cmd), repo), "Conventional"
        )


class TestUnquotedNewlines:
    """An unquoted newline separates commands, like `;`."""

    def test_commit_after_newline_is_checked(self, repo):
        cmd = 'git status\ngit commit -m "bad"'
        assert_block(
            run_hook("commit-gate.sh", bash_payload(cmd), repo), "Conventional"
        )

    def test_commit_on_protected_branch_after_newline(self, repo):
        _git(repo, "checkout", "-q", "-B", "dev")
        cmd = 'git status\ngit commit -m "feat: fine"'
        assert_block(run_hook("commit-gate.sh", bash_payload(cmd), repo), "'dev'")

    def test_pr_create_after_newline_is_checked(self, repo):
        cmd = 'git status\ngh pr create --base dev --title "bad title"'
        assert_block(run_hook("pr-title-gate.sh", bash_payload(cmd), repo), "title")

    def test_newlines_inside_quotes_stay_in_the_message(self, repo):
        cmd = 'git commit -m "feat: fine" -m "line one\ngit commit -m bad\nline three"'
        assert_allow(run_hook("commit-gate.sh", bash_payload(cmd), repo))

    def test_line_continuation_is_not_a_separator(self, repo):
        cmd = 'git commit \\\n  -m "feat: fine"'
        assert_allow(run_hook("commit-gate.sh", bash_payload(cmd), repo))


@pytest.mark.skipif(
    shutil.which("ruff") is None or shutil.which("black") is None,
    reason="precheck backend tests need ruff and black",
)
class TestPrecheckSnapshot:
    """Checks run on the committed tree, which is what the cache key names."""

    @pytest.fixture
    def pre_repo(self, repo):
        _git(repo, "update-ref", "refs/remotes/origin/dev", "HEAD")
        (repo / "backend").mkdir()
        return repo

    def commit_py(self, repo, content):
        (repo / "backend" / "mod.py").write_text(content)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "feat: change")

    def gate(self, repo, command="git push"):
        return run_hook("precheck-gate.sh", bash_payload(command), repo)

    def cache_entries(self, tmp_path):
        cache = tmp_path / "xdg-cache" / "bookshelf-claude-hooks"
        return list(cache.iterdir()) if cache.exists() else []

    def test_unformatted_commit_fixed_only_in_working_copy_still_blocks(
        self, pre_repo, tmp_path
    ):
        self.commit_py(pre_repo, "x   =   1\n")
        (pre_repo / "backend" / "mod.py").write_text("X = 1\n")  # uncommitted fix
        assert_block(self.gate(pre_repo), "Pre-push checks failed")
        assert self.cache_entries(tmp_path) == []

    def test_clean_commit_with_dirty_working_copy_passes(self, pre_repo):
        self.commit_py(pre_repo, "X = 1\n")
        (pre_repo / "backend" / "mod.py").write_text("x   =   1\n")  # uncommitted mess
        assert_allow(self.gate(pre_repo))

    def test_git_dash_c_push_uses_that_repo(self, pre_repo, tmp_path):
        self.commit_py(pre_repo, "x   =   1\n")
        cmd = f"git -C {pre_repo} push"
        assert_block(
            run_hook("precheck-gate.sh", bash_payload(cmd), tmp_path.parent), "failed"
        )

    def test_push_after_newline_is_checked(self, pre_repo):
        self.commit_py(pre_repo, "x   =   1\n")
        assert_block(self.gate(pre_repo, "git status\ngit push"), "failed")


# ── Strict isolation and push refs (PR #794 rounds 2-4) ───────────────────

ISOLATE = "run git commit / git push on its own"


class TestCommitIsolation:
    """A commit may share its command only with git add/status/diff/log."""

    def gate(self, repo, cmd):
        return run_hook("commit-gate.sh", bash_payload(cmd), repo)

    @pytest.mark.parametrize(
        "cmd",
        [
            'git add -A && git commit -m "fix: x"',
            'git status && git diff\ngit add . && git commit -m "fix: x" && git log -1',
            'git commit -m "fix: x" && git status',
        ],
    )
    def test_whitelisted_companions_allowed(self, repo, cmd):
        assert_allow(self.gate(repo, cmd))

    @pytest.mark.parametrize(
        "cmd",
        [
            # Codex round 4 (a): cd retargets the repo.
            'cd /somewhere && git commit -m "fix: x"',
            'pushd wt && git commit -m "fix: x"',
            'GIT_DIR=/x/.git git commit -m "fix: x"',
            'export GIT_DIR=/x && git commit -m "fix: x"',
            'git --git-dir=/x/.git commit -m "fix: x"',
            "bash -c 'git commit -m \"bad\"'",
            'git -c core.hooksPath=/x commit -m "fix: x"',  # round 9: -c overrides
            'command git commit -m "bad"',  # Codex round 8: argv wrappers
            'env git commit -m "bad"',
            'sh -c "git commit -m bad"',
            'eval "git commit -m bad"',
            '(git commit -m "fix: x")',
            'echo hi; git commit -m "fix: x"',
            # Branch switches (rounds 3-4), HEAD changes (round 2), second commit.
            'git checkout dev && git commit -m "fix: x"',
            'git checkout -b feature/new && git commit -m "fix: x"',
            'git switch dev && git commit -m "fix: x"',
            'git worktree add ../wt && git commit -m "fix: x"',
            'git stash branch b && git commit -m "fix: x"',
            'git commit -m "fix: one" && git commit -m "fix: two"',
            'git commit -m "fix: one" && git push',
            'git commit -m "fix: x" && git checkout dev',
        ],
    )
    def test_other_commands_block(self, repo, cmd):
        assert_block(self.gate(repo, cmd), ISOLATE)

    def test_file_restore_alone_is_not_a_commit(self, repo):
        assert_allow(self.gate(repo, "git checkout -- README.txt"))

    def test_protected_branch_still_blocks_when_isolated(self, repo):
        _git(repo, "checkout", "-q", "-B", "dev")
        cmd = 'git add -A && git commit -m "fix: x"'
        assert_block(self.gate(repo, cmd), "'dev'")

    def test_bad_message_still_blocks_when_isolated(self, repo):
        assert_block(
            self.gate(repo, 'git add -A && git commit -m "bad"'), "Conventional"
        )


class TestPushIsolationAndRefs:
    @pytest.fixture
    def pre_repo(self, repo):
        _git(repo, "update-ref", "refs/remotes/origin/dev", "HEAD")
        _git(repo, "branch", "bad")
        return repo

    def gate(self, repo, cmd):
        return run_hook("precheck-gate.sh", bash_payload(cmd), repo)

    @pytest.mark.parametrize(
        "cmd",
        [
            'git commit -m "fix: x" && git push',  # round 2
            "git switch bad && git push",  # round 4 (c)
            "git checkout bad && git push",
            "cd /somewhere && git push",
            "GIT_DIR=/x git push",
            "git fetch && git push",
            "git push && git push",
            "git push && git push origin --delete old",
            "bash -c 'git push'",
            "command git push",
            "git -c remote.origin.push=bad push origin",  # round 9
            "git --config-env=remote.origin.push=X push origin",
            "env git push origin other",
            "(git push)",
        ],
    )
    def test_push_must_be_alone(self, pre_repo, cmd):
        assert_block(self.gate(pre_repo, cmd), ISOLATE)

    @pytest.mark.parametrize(
        "cmd",
        [
            # Codex round 16: a command after a stdin heredoc is its own segment.
            "git commit -F - <<'EOF'\nfix: valid\nEOF\ngit push origin bad",
            "git commit -F - <<'EOF' && git push origin bad\nfix: valid\nEOF",
        ],
    )
    def test_command_after_stdin_heredoc_is_seen(self, pre_repo, cmd):
        assert_block(self.gate(pre_repo, cmd), ISOLATE)

    @pytest.mark.parametrize(
        "cmd",
        [
            "git push origin bad",  # round 4 (b): not the current branch
            "git push origin bad:bad",
            "git push origin +bad",
            "git push -u origin bad",
            "git push origin feature/x bad",
            "git push origin tag v1",
        ],
    )
    def test_push_of_other_source_ref_blocks(self, pre_repo, cmd):
        assert_block(self.gate(pre_repo, cmd), "check out ")

    @pytest.mark.parametrize(
        "cmd",
        [
            "git push",
            "git push origin",
            "git push origin HEAD",
            "git push origin feature/x",
            "git push origin refs/heads/feature/x",
            "git push origin HEAD:feature/x",
            "git push origin feature/x:feature/y",
            "git push -u origin feature/x",
            "git push --set-upstream origin HEAD",
            "git push --force-with-lease origin feature/x",
            "git status && git push",
            "git push && git log -1",
        ],
    )
    def test_push_of_current_branch_allowed(self, pre_repo, cmd):
        assert_allow(self.gate(pre_repo, cmd))

    @pytest.mark.parametrize(
        "cmd",
        [
            "git push origin --delete bad",
            "git push origin :bad",
            "git push origin :bad :other",
        ],
    )
    def test_delete_pushes_skip_precheck(self, pre_repo, cmd):
        assert_allow(self.gate(pre_repo, cmd))

    def test_detached_head_only_allows_head(self, pre_repo):
        _git(pre_repo, "checkout", "-q", "--detach")
        assert_block(self.gate(pre_repo, "git push origin feature/x"), "check out ")
        assert_allow(self.gate(pre_repo, "git push origin HEAD"))


@pytest.mark.skipif(
    shutil.which("ruff") is None or shutil.which("black") is None,
    reason="precheck backend tests need ruff and black",
)
class TestPrecheckStillRunsAfterRefChecks:
    def test_unformatted_current_branch_push_blocks(self, repo):
        _git(repo, "update-ref", "refs/remotes/origin/dev", "HEAD")
        (repo / "backend").mkdir()
        (repo / "backend" / "mod.py").write_text("x   =   1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "feat: change")
        result = run_hook(
            "precheck-gate.sh", bash_payload("git push -u origin feature/x"), repo
        )
        assert_block(result, "Pre-push checks failed")


class TestAllPrCreates:
    """Every `gh pr create` in one command line is validated."""

    def test_second_pr_create_title_is_checked(self, repo):
        cmd = (
            'gh pr create --base dev --title "fix: ok" && '
            'gh pr create --base dev --title "bad title"'
        )
        assert_block(run_hook("pr-title-gate.sh", bash_payload(cmd), repo), "title")

    def test_second_pr_create_base_is_checked(self, repo):
        cmd = (
            'gh pr create --base dev --title "fix: ok"; '
            'gh pr create --base staging --title "fix: ok"'
        )
        assert_block(run_hook("pr-title-gate.sh", bash_payload(cmd), repo), "staging")


class TestGhInheritedFlags:
    """`-R/--repo` may come before or after `pr`; the create is still checked."""

    @pytest.mark.parametrize(
        "prefix",
        [
            "gh -R owner/repo pr create",
            "gh --repo owner/repo pr create",
            "gh --repo=owner/repo pr create",
            "gh -Rowner/repo pr create",
            "gh pr -R owner/repo create",
        ],
    )
    def test_inherited_repo_flag_still_validated(self, repo, prefix):
        cmd = f"{prefix} --title invalid --base main --head feature/x"
        assert_block(run_hook("pr-title-gate.sh", bash_payload(cmd), repo), "title")

    def test_inherited_repo_flag_valid_create_allowed(self, repo):
        cmd = 'gh -R owner/repo pr create --base dev --title "fix: ok"'
        assert_allow(run_hook("pr-title-gate.sh", bash_payload(cmd), repo))

    def test_env_assignment_prefix_still_validated(self, repo):
        # Codex round 12.
        cmd = "GH_TOKEN=x gh pr create --title invalid --base main --head feature/x"
        assert_block(run_hook("pr-title-gate.sh", bash_payload(cmd), repo), "title")

    @pytest.mark.parametrize(
        "cmd",
        [
            'env gh pr create --base main --title "fix: x"',
            'command gh pr new --base main --title "fix: x"',
            "bash -c 'gh pr create --base main --title x'",
        ],
    )
    def test_wrapped_gh_pr_create_fails_closed(self, repo, cmd):
        assert_block(run_hook("pr-title-gate.sh", bash_payload(cmd), repo), "parse")

    @pytest.mark.parametrize(
        "cmd, needle",
        [
            # Codex round 15: other flags' values are consumed, never re-read.
            (
                'gh pr create --base main --head feature/x --title "fix: x" --body "--base=dev"',
                "base 'main'",
            ),
            ('gh pr create --base dev -b "--title=fix: ok" --title "bad"', "title"),
            ('gh pr create --base dev --label "-Bdev" -t "Bad"', "title"),
            ('gh pr create --base dev --title "fix: x" --bogus', "parse"),
            ('gh pr create --base dev --title "fix: x" stray', "parse"),
        ],
    )
    def test_flag_values_are_not_flags(self, repo, cmd, needle):
        assert_block(run_hook("pr-title-gate.sh", bash_payload(cmd), repo), needle)

    @pytest.mark.parametrize(
        "cmd",
        [
            'gh pr create --base dev --title "fix: x" --body "--base=main" --draft',
            'gh pr create -d -B dev -t "fix: x" -b body -l ci -r someone -a me',
            'gh pr create --base dev --title "fix: x" --body-file pr.md --no-maintainer-edit',
        ],
    )
    def test_full_flag_set_allowed(self, repo, cmd):
        assert_allow(run_hook("pr-title-gate.sh", bash_payload(cmd), repo))

    def test_unknown_flag_before_create_fails_closed(self, repo):
        cmd = 'gh --weird pr create --base dev --title "fix: ok"'
        assert_block(run_hook("pr-title-gate.sh", bash_payload(cmd), repo), "parse")

    def test_other_gh_commands_ignored(self, repo):
        assert_allow(
            run_hook("pr-title-gate.sh", bash_payload("gh -R o/r pr list"), repo)
        )


class TestImplicitAndMatchingPushRefs:
    @pytest.fixture
    def pre_repo(self, repo):
        _git(repo, "update-ref", "refs/remotes/origin/dev", "HEAD")
        return repo

    def gate(self, repo, cmd):
        return run_hook("precheck-gate.sh", bash_payload(cmd), repo)

    @pytest.mark.parametrize(
        "cmd", ["git push origin", "git push", "git push -u origin"]
    )
    def test_configured_remote_push_blocks(self, pre_repo, cmd):
        _git(pre_repo, "config", "remote.origin.push", "bad")
        assert_block(self.gate(pre_repo, cmd), "remote.origin.push is configured")

    def test_configured_push_uses_branch_remote_when_unnamed(self, pre_repo):
        _git(pre_repo, "config", "branch.feature/x.remote", "upstream")
        _git(pre_repo, "config", "remote.upstream.push", "bad")
        assert_block(
            self.gate(pre_repo, "git push"), "remote.upstream.push is configured"
        )

    @pytest.mark.parametrize(
        "cmd", ["git push origin", "git push", "git push origin feature/x"]
    )
    def test_mirror_remote_blocks(self, pre_repo, cmd):
        _git(pre_repo, "config", "remote.origin.mirror", "true")
        assert_block(self.gate(pre_repo, cmd), "remote.origin.mirror is set")

    @pytest.mark.parametrize(
        "key", ["branch.feature/x.pushRemote", "remote.pushDefault"]
    )
    def test_push_remote_override_is_checked(self, pre_repo, key):
        # Codex round 10: pushRemote / pushDefault beat branch.<b>.remote.
        _git(pre_repo, "config", "branch.feature/x.remote", "origin")
        _git(pre_repo, "config", key, "backup")
        _git(pre_repo, "config", "remote.backup.push", "bad")
        assert_block(
            self.gate(pre_repo, "git push"), "remote.backup.push is configured"
        )

    def test_push_remote_beats_push_default(self, pre_repo):
        _git(pre_repo, "config", "remote.pushDefault", "other")
        _git(pre_repo, "config", "branch.feature/x.pushRemote", "backup")
        _git(pre_repo, "config", "remote.backup.mirror", "true")
        assert_block(self.gate(pre_repo, "git push"), "remote.backup.mirror is set")

    def test_configured_push_ignored_with_explicit_refspec(self, pre_repo):
        _git(pre_repo, "config", "remote.origin.push", "bad")
        assert_allow(self.gate(pre_repo, "git push origin feature/x"))

    @pytest.mark.parametrize("value", ["matching", "nothing"])
    def test_unsafe_push_default_blocks(self, pre_repo, value):
        _git(pre_repo, "config", "push.default", value)
        assert_block(self.gate(pre_repo, "git push origin"), f"push.default={value}")

    @pytest.mark.parametrize("value", ["simple", "current", "upstream"])
    def test_safe_push_default_allowed(self, pre_repo, value):
        _git(pre_repo, "config", "push.default", value)
        assert_allow(self.gate(pre_repo, "git push origin"))

    @pytest.mark.parametrize(
        "cmd", ["git push origin :", "git push origin +:", "git push origin + :"]
    )
    def test_bare_colon_is_matching_not_delete(self, pre_repo, cmd):
        assert_block(self.gate(pre_repo, cmd), "check out ")

    def test_real_delete_refspecs_still_allowed(self, pre_repo):
        assert_allow(self.gate(pre_repo, "git push origin +:bad"))
        assert_allow(self.gate(pre_repo, "git push origin :bad"))


PARSE_MSG = "couldn't parse this command safely"


class TestFailClosed:
    def commit(self, repo, cmd):
        return run_hook("commit-gate.sh", bash_payload(cmd), repo)

    def test_comment_with_apostrophe_before_bad_commit_on_dev_blocks(self, repo):
        _git(repo, "checkout", "-q", "-B", "dev")
        cmd = "# Let's commit\ngit commit -m 'invalid'"
        assert_block(self.commit(repo, cmd), "'dev'")

    def test_comment_with_apostrophe_before_bad_message_blocks(self, repo):
        cmd = "# Let's commit\ngit commit -m 'invalid'"
        assert_block(self.commit(repo, cmd), "Conventional")

    def test_comment_with_apostrophe_before_valid_commit_allowed(self, repo):
        cmd = "# Let's commit this\ngit commit -m 'fix: valid'"
        assert_allow(self.commit(repo, cmd))

    def test_trailing_comment_allowed_and_hash_in_message_kept(self, repo):
        assert_allow(self.commit(repo, 'git commit -m "fix: issue #12" # it\'s fine'))
        assert_block(self.commit(repo, 'git commit -m "bad #12"'), "Conventional")

    @pytest.mark.parametrize(
        "cmd",
        [
            'git commit -m "unterminated',
            "git commit -m 'fix: x' -m 'oops",
            'echo "x" && git commit -m "bad',
        ],
    )
    def test_malformed_quote_blocks(self, repo, cmd):
        assert_block(self.commit(repo, cmd), PARSE_MSG)

    def test_unrelated_malformed_command_is_not_blocked(self, repo):
        assert_allow(self.commit(repo, "echo 'unterminated"))

    def test_push_parse_failure_blocks(self, repo):
        result = run_hook(
            "precheck-gate.sh", bash_payload("git push origin 'bad"), repo
        )
        assert_block(result, PARSE_MSG)

    def test_pr_create_parse_failure_blocks(self, repo):
        cmd = 'gh pr create --base dev --title "feat: x'
        assert_block(run_hook("pr-title-gate.sh", bash_payload(cmd), repo), PARSE_MSG)

    def test_missing_python_blocks_instead_of_allowing(self, repo, tmp_path):
        bin_dir = tmp_path / "nopy"
        bin_dir.mkdir()
        for tool in (
            "bash",
            "git",
            "jq",
            "cat",
            "dirname",
            "pwd",
            "sed",
            "head",
            "tr",
            "date",
        ):
            found = shutil.which(tool)
            if found:
                (bin_dir / tool).symlink_to(found)
        result = subprocess.run(
            [str(bin_dir / "bash"), str(HOOKS / "commit-gate.sh")],
            cwd=repo,
            input=json.dumps(bash_payload('git commit -m "bad"')),
            capture_output=True,
            text=True,
            env={"PATH": str(bin_dir), "HOME": str(tmp_path)},
            check=False,
        )
        assert_block(result, PARSE_MSG)


class TestPushFlagWhitelist:
    @pytest.fixture
    def pre_repo(self, repo):
        _git(repo, "update-ref", "refs/remotes/origin/dev", "HEAD")
        return repo

    def gate(self, repo, cmd):
        return run_hook("precheck-gate.sh", bash_payload(cmd), repo)

    @pytest.mark.parametrize(
        "flag",
        [
            "--branches",
            "--all",
            "--mirror",
            "--tags",
            "--follow-tags",
            "-f",
            "--force",
            "--prune",
            "--push-option=x",
            "-o",
            "--repo=other",
            "--exec=x",
            "--no-verify",
            "--atomic",
            "-uf",
        ],
    )
    def test_unlisted_flags_block(self, pre_repo, flag):
        result = self.gate(pre_repo, f"git push {flag} origin feature/x")
        assert_block(result, f"git push flag {flag} isn't allowed by the gate")

    def test_unlisted_flag_blocks_even_for_deletes(self, pre_repo):
        assert_block(
            self.gate(pre_repo, "git push --force origin --delete bad"), "isn't allowed"
        )

    @pytest.mark.parametrize(
        "flag",
        [
            "-u",
            "--set-upstream",
            "--force-with-lease",
            "--force-with-lease=feature/x:abc",
            "--force-if-includes",
            "-v",
            "--verbose",
            "-q",
            "--quiet",
            "-n",
            "--dry-run",
            "--progress",
            "--no-progress",
            "-uv",
        ],
    )
    def test_listed_flags_allowed(self, pre_repo, flag):
        assert_allow(self.gate(pre_repo, f"git push {flag} origin feature/x"))

    def test_delete_flag_allowed(self, pre_repo):
        assert_allow(self.gate(pre_repo, "git push -d origin bad"))


@pytest.mark.skipif(shutil.which("npm") is None, reason="needs npm")
class TestPrecheckTypecheckTrigger:
    """Typecheck runs on any frontend change, not only surviving JS/TS files."""

    @pytest.fixture
    def pre_repo(self, repo):
        fe = repo / "frontend"
        (fe / "node_modules" / ".bin").mkdir(parents=True)
        tsc = fe / "node_modules" / ".bin" / "tsc"
        tsc.write_text("#!/bin/sh\nexit 0\n")
        tsc.chmod(0o755)
        (fe / "package.json").write_text(
            '{"scripts": {"typecheck": "echo TS2307 missing module; exit 1"}}\n'
        )
        (fe / "gone.ts").write_text("export const x = 1;\n")
        (repo / ".gitignore").write_text("node_modules/\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "chore: frontend")
        _git(repo, "update-ref", "refs/remotes/origin/dev", "HEAD")
        return repo

    def gate(self, repo):
        return run_hook(
            "precheck-gate.sh", bash_payload("git push origin feature/x"), repo
        )

    def test_deletion_only_commit_runs_typecheck(self, pre_repo):
        _git(pre_repo, "rm", "-q", "frontend/gone.ts")
        _git(pre_repo, "commit", "-qm", "refactor: drop module")
        assert_block(self.gate(pre_repo), "typecheck")

    def test_dirty_frontend_blocks_typecheck(self, pre_repo):
        # Codex round 13: an uncommitted fix must not vouch for HEAD.
        _git(pre_repo, "rm", "-q", "frontend/gone.ts")
        _git(pre_repo, "commit", "-qm", "refactor: drop module")
        (pre_repo / "frontend" / "gone.ts").write_text("export const x = 1;\n")
        result = self.gate(pre_repo)
        assert_block(result, "uncommitted or untracked changes")

    def test_tsconfig_only_change_runs_typecheck(self, pre_repo):
        (pre_repo / "frontend" / "tsconfig.json").write_text("{}\n")
        _git(pre_repo, "add", "-A")
        _git(pre_repo, "commit", "-qm", "chore: tsconfig")
        assert_block(self.gate(pre_repo), "typecheck")

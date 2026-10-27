"""Tests for the destructive-migration scanner and its workflow (#687)."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "migration_guard" / "check_destructive_migrations.py"
WORKFLOW = REPO / ".github" / "workflows" / "migration-guard.yml"
VERSIONS = REPO / "backend" / "alembic" / "versions"

_spec = importlib.util.spec_from_file_location("check_destructive_migrations", SCRIPT)
guard = importlib.util.module_from_spec(_spec)
sys.modules["check_destructive_migrations"] = guard
_spec.loader.exec_module(guard)

HEAD = "import sqlalchemy as sa\nfrom alembic import op\n\n\n"


def migration(upgrade_body: str, downgrade_body: str = "    pass") -> str:
    return f"{HEAD}def upgrade() -> None:\n{upgrade_body}\n\n\ndef downgrade() -> None:\n{downgrade_body}\n"


def scan(upgrade_body: str, downgrade_body: str = "    pass") -> list[str]:
    return guard.scan_source(migration(upgrade_body, downgrade_body), "m.py")


# (upgrade body, expected operation text)
DESTRUCTIVE = [
    ('    op.drop_column("users", "legacy")', "op.drop_column"),
    ('    op.drop_table("legacy")', "op.drop_table"),
    ('    op.rename_table("a", "b")', "op.rename_table"),
    ('    op.alter_column("b", "x", new_column_name="y")', "new_column_name"),
    ('    op.alter_column("u", "n", type_=sa.String(10))', "type_"),
    ('    op.alter_column("u", "n", nullable=False)', "nullable=False"),
    ('    op.execute("drop table legacy")', "destructive SQL"),
    ('    op.execute(sa.text("TRUNCATE ai_usage"))', "destructive SQL"),
    ('    op.execute("DELETE FROM t")', "destructive SQL"),
    ('    op.execute("ALTER TABLE t ALTER COLUMN c TYPE integer")', "destructive SQL"),
    ('    op.execute("alter table t drop column c")', "destructive SQL"),
    ('    op.execute(sa.delete(sa.table("t")))', "deleting rows"),
    (
        '    with op.batch_alter_table("users") as batch_op:\n        batch_op.drop_column("old")',
        "batch_op.drop_column",
    ),
    (
        (
            '    with op.batch_alter_table("users") as b:\n'
            '        b.alter_column("c", type_=sa.Integer())'
        ),
        "b.alter_column",
    ),
    (
        (
            '    with op.batch_alter_table("users") as b:\n'
            '        b.alter_column("c", nullable=False)'
        ),
        "nullable=False",
    ),
]


@pytest.mark.parametrize("body,expected", DESTRUCTIVE)
def test_destructive_upgrade_is_flagged(body, expected):
    findings = scan(body)
    assert len(findings) == 1
    assert findings[0].startswith("m.py:")
    assert expected in findings[0]


def test_finding_has_file_line_operation_format():
    source = migration(
        '    op.add_column("u", sa.Column("a", sa.Text()))\n    op.drop_column("u", "x")'
    )
    (finding,) = guard.scan_source(source, "backend/alembic/versions/0021_x.py")
    line = source.splitlines().index('    op.drop_column("u", "x")') + 1
    assert finding == f"backend/alembic/versions/0021_x.py:{line}: op.drop_column"


def test_additive_migration_passes():
    body = (
        '    op.add_column("u", sa.Column("nick", sa.Text(), nullable=True))\n'
        '    op.create_table("t", sa.Column("id", sa.Integer(), primary_key=True))\n'
        '    op.create_index("ix_u_nick", "u", ["nick"])\n'
        '    op.alter_column("u", "nick", nullable=True)\n'
        '    op.alter_column("u", "nick", server_default="x")\n'
        "    op.execute(\"UPDATE u SET nick = 'x'\")\n"
        '    with op.batch_alter_table("u") as batch_op:\n'
        '        batch_op.add_column(sa.Column("z", sa.Text()))'
    )
    assert scan(body) == []


def test_index_and_constraint_drops_are_allowed():
    body = (
        '    op.drop_index("ix_old", table_name="u")\n'
        '    op.drop_constraint("uq_old", "u")\n'
        '    with op.batch_alter_table("u") as batch_op:\n'
        '        batch_op.drop_index("ix_old")\n'
        '        batch_op.drop_constraint("uq_old")'
    )
    assert scan(body) == []


def test_destructive_only_in_downgrade_passes():
    down = '    op.drop_column("u", "nick")\n    op.drop_table("x")\n    op.execute("DROP TABLE y")'
    assert scan('    op.add_column("u", sa.Column("nick", sa.Text()))', down) == []


def test_multiple_findings_sorted_by_line():
    findings = scan('    op.drop_table("a")\n    op.drop_column("u", "x")')
    assert [f.split(":")[1] for f in findings] == sorted(
        f.split(":")[1] for f in findings
    )
    assert len(findings) == 2


def test_other_receivers_are_ignored():
    assert scan('    session.drop_column("u", "x")') == []


def test_unparseable_file_is_reported():
    (finding,) = guard.scan_source("def upgrade(:\n", "bad.py")
    assert finding.startswith("bad.py:1: cannot parse migration")


def _run(tmp_path, source, *flags):
    f = tmp_path / "0099_x.py"
    f.write_text(source)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *flags, str(f)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_cli_fails_without_approval_and_passes_with_it(tmp_path):
    source = migration('    op.drop_column("u", "x")')
    res = _run(tmp_path, source)
    assert res.returncode == 1
    assert "0099_x.py:" in res.stdout and "op.drop_column" in res.stdout
    assert "destructive-migration-approved" in res.stdout
    ok = _run(tmp_path, source, "--approved")
    assert ok.returncode == 0
    assert "op.drop_column" in ok.stdout


def test_cli_passes_additive_and_empty_input(tmp_path):
    assert (
        _run(
            tmp_path, migration('    op.add_column("u", sa.Column("a", sa.Text()))')
        ).returncode
        == 0
    )
    assert subprocess.run([sys.executable, str(SCRIPT)], check=False).returncode == 0


def test_existing_migrations_flagged_are_the_known_historical_ones():
    flagged = {
        path.name[:4]
        for path in sorted(VERSIONS.glob("*.py"))
        if guard.scan_source(path.read_text(), path.name)
    }
    # 0015 deletes duplicate rows via helpers; 0018/0019 are the contract steps of
    # #594 and #540. CI scans changed files only, so history is not re-gated.
    assert flagged == {"0015", "0018", "0019"}


def test_fstring_sql_is_joined_before_matching():
    both = '    op.execute(f"ALTER TABLE {table} ALTER COLUMN {col} TYPE varchar(10)")'
    assert len(scan(both)) == 1
    table_only = (
        '    op.execute(f"ALTER TABLE {table} ALTER COLUMN c TYPE varchar(10)")'
    )
    assert len(scan(table_only)) == 1


@pytest.mark.parametrize(
    "stmt",
    [
        "ALTER TABLE a RENAME TO b",
        "ALTER TABLE a RENAME COLUMN x TO y",
        "DROP SCHEMA s",
        "DROP VIEW v",
        "DROP TYPE t",
        "ALTER TABLE a ALTER c TYPE text",
    ],
)
def test_more_sql_patterns(stmt):
    assert len(scan(f'    op.execute("{stmt}")')) == 1


def test_alter_type_does_not_span_statements():
    body = '    op.execute("ALTER TABLE a ADD COLUMN c text; SELECT 1 AS type")'
    assert scan(body) == []


@pytest.mark.parametrize(
    "body",
    [
        '    op.get_bind().execute(sa.text("DROP TABLE t"))',
        '    conn = op.get_bind()\n    conn.execute(sa.text("TRUNCATE t"))',
        '    connection.execute("DELETE FROM t")',
    ],
)
def test_execute_on_any_receiver(body):
    assert len(scan(body)) == 1


def test_helpers_are_followed_transitively_and_cycle_safe():
    source = (
        HEAD
        + "def _run(conn, sql):\n    conn.execute(sa.text(sql))\n\n\n"
        + "def _a(conn):\n    _run(conn, 'DROP TABLE t')\n    _b(conn)\n\n\n"
        + "def _b(conn):\n    _a(conn)\n    op.drop_column('u', 'c')\n\n\n"
        + "def upgrade() -> None:\n    _a(op.get_bind())\n\n\n"
        + "def downgrade() -> None:\n    pass\n"
    )
    findings = guard.scan_source(source, "m.py")
    assert len(findings) == 2
    assert any("op.drop_column" in f for f in findings)


def test_unused_helper_is_not_scanned():
    source = (
        HEAD
        + "def _unused():\n    op.drop_table('t')\n\n\n"
        + "def upgrade() -> None:\n    pass\n\n\n"
        + "def downgrade() -> None:\n    _unused()\n"
    )
    assert guard.scan_source(source, "m.py") == []


def test_module_constants_and_concatenation_are_resolved():
    source = (
        HEAD
        + 'SQL = "DROP TABLE t"\nPART = "TRUNCATE " + "t"\n\n\n'
        + "def upgrade() -> None:\n    op.execute(SQL)\n    op.execute(sa.text(PART))\n"
        + '    op.execute("DEL" + "ETE FROM t")\n\n\n'
        + "def downgrade() -> None:\n    pass\n"
    )
    assert len(guard.scan_source(source, "m.py")) == 3


def test_op_alias_is_tracked():
    source = (
        "import sqlalchemy as sa\nfrom alembic import op as alembic_op\n\n\n"
        "def upgrade() -> None:\n    alembic_op.drop_column('u', 'c')\n\n\n"
        "def downgrade() -> None:\n    pass\n"
    )
    assert len(guard.scan_source(source, "m.py")) == 1


def test_missing_upgrade_fails_closed():
    (finding,) = guard.scan_source("x = 1\n", "m.py")
    assert "no top-level upgrade()" in finding


def test_workflow_diff_selects_renamed_files(tmp_path):
    text = WORKFLOW.read_text()
    assert "--no-renames" in text
    line = next(ln for ln in text.splitlines() if "git diff --name-only" in ln)
    cmd = line.split("files=$(", 1)[1].rsplit(")", 1)[0]
    cmd = cmd.replace("origin/${BASE_REF}", "base")

    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q", "-b", "base")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    versions = tmp_path / "backend" / "alembic" / "versions"
    versions.mkdir(parents=True)
    body = "# filler line\n" * 40
    (versions / "0020_a.py").write_text(body + "def upgrade():\n    pass\n")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    git("checkout", "-q", "-b", "pr")
    git(
        "mv", "backend/alembic/versions/0020_a.py", "backend/alembic/versions/0021_a.py"
    )
    renamed = versions / "0021_a.py"
    renamed.write_text(renamed.read_text() + "    op.drop_column('u', 'c')\n")
    git("add", "-A")
    git("commit", "-q", "-m", "pr")
    out = subprocess.run(
        cmd, shell=True, cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout
    assert out.split() == ["backend/alembic/versions/0021_a.py"]


def test_workflow_runs_on_label_changes_and_checks_the_label():
    text = WORKFLOW.read_text()
    assert "labeled, unlabeled" in text
    assert (
        "contains(github.event.pull_request.labels.*.name, 'destructive-migration-approved')"
        in text
    )
    assert "fetch-depth: 0" in text
    assert "persist-credentials: false" in text
    assert "branches: [dev, main]" in text
    assert '"$BASE_REF" = "main"' in text
    assert "Release PR: migrations were already scanned" in text
    assert "git diff --name-only" in text and "backend/alembic/versions/" in text


@pytest.mark.parametrize(
    "body",
    [
        '    stmt = "DROP TABLE x"\n    op.execute(stmt)',
        '    stmt = sa.text("DELETE FROM x")\n    op.execute(stmt)',
        '    base = "DROP "\n    stmt = base + "TABLE x"\n    op.get_bind().execute(sa.text(stmt))',
    ],
)
def test_local_variables_are_resolved(body):
    assert len(scan(body)) == 1


def test_reused_local_variable_keeps_earlier_values():
    # Codex on #699: the last assignment must not hide an earlier DROP.
    body = (
        '    sql = "DROP TABLE users"\n    op.execute(sql)\n'
        '    sql = "SELECT 1"\n    op.execute(sql)'
    )
    assert scan(body)


def test_reused_local_variable_stays_clean_when_all_values_are():
    body = '    sql = "SELECT 1"\n    op.execute(sql)\n    sql = "SELECT 2"\n    op.execute(sql)'
    assert scan(body) == []


def test_local_variables_in_followed_helper():
    source = (
        HEAD
        + "def _helper():\n    stmt = 'TRUNCATE t'\n    op.execute(stmt)\n\n\n"
        + "def upgrade() -> None:\n    _helper()\n\n\n"
        + "def downgrade() -> None:\n    pass\n"
    )
    assert len(guard.scan_source(source, "m.py")) == 1


@pytest.mark.parametrize(
    "stmt",
    [
        "ALTER TABLE a ADD CONSTRAINT fk FOREIGN KEY (b) REFERENCES c (id) ON DELETE CASCADE",
        "ALTER TABLE a ADD CONSTRAINT fk FOREIGN KEY (b) REFERENCES c (id) ON DELETE SET NULL",
        "UPDATE t SET note = 'delete me'",
    ],
)
def test_on_delete_and_delete_in_text_are_not_flagged(stmt):
    assert scan(f'    op.execute("{stmt}")') == []


@pytest.mark.parametrize("stmt", ["DELETE FROM t", "delete from t", "DELETE\n  FROM t"])
def test_delete_from_is_flagged(stmt):
    assert len(scan(f'    op.execute("{stmt}")')) == 1


def test_ruleset_requires_migration_guard():
    ruleset = REPO / ".github" / "rulesets" / "protect-main-dev.json"
    assert '"context": "migration-guard"' in ruleset.read_text()

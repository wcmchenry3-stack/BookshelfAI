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
    flagged = set()
    for path in sorted(VERSIONS.glob("*.py")):
        if guard.scan_source(path.read_text(), path.name):
            flagged.add(path.name[:4])
    # 0018/0019 are the contract migrations of #594 and #540; CI scans changed files only.
    assert {"0018", "0019"} <= flagged


def test_workflow_runs_on_label_changes_and_checks_the_label():
    text = WORKFLOW.read_text()
    assert "labeled, unlabeled" in text
    assert (
        "contains(github.event.pull_request.labels.*.name, 'destructive-migration-approved')"
        in text
    )
    assert "fetch-depth: 0" in text
    assert "git diff --name-only" in text and "backend/alembic/versions/" in text

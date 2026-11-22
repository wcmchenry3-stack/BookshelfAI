"""The Alembic revision template renders valid, lint-clean migrations (#843).

``alembic revision`` (and ``--autogenerate``, which docs/data-model.md tells
developers to run) needs ``alembic/script.py.mako``. Without it the command
fails with FileNotFoundError. These tests render revisions through Alembic's
own command and script API into a temporary versions directory, so the real
``alembic/versions/`` is never touched and no database is needed.
"""

import subprocess
import sys
from pathlib import Path

import black
from alembic.config import Config
from alembic.script import ScriptDirectory

from alembic import command

BACKEND = Path(__file__).resolve().parents[2]
ALEMBIC_DIR = BACKEND / "alembic"
PARENT_REVISION = "base0000"
NEW_REVISION = "test0000"


def _config(versions_dir: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("version_locations", str(versions_dir))
    cfg.set_main_option("version_path_separator", "os")
    return cfg


def _write_revision(versions: Path, rev_id: str, down: str | None) -> None:
    versions.mkdir(exist_ok=True)
    (versions / f"{rev_id}_{rev_id}.py").write_text(
        f'revision = "{rev_id}"\ndown_revision = {down!r}\n'
        "branch_labels = None\ndepends_on = None\n\n\n"
        "def upgrade() -> None:\n    pass\n\n\n"
        "def downgrade() -> None:\n    pass\n",
        encoding="utf-8",
    )


def _generated(versions: Path, rev_id: str) -> Path:
    found = list(versions.glob(f"{rev_id}_*.py"))
    assert len(found) == 1, found
    return found[0]


def _render(tmp_path: Path, **body) -> str:
    """Render NEW_REVISION on top of a parent revision; return its source."""
    versions = tmp_path / "versions"
    _write_revision(versions, PARENT_REVISION, None)
    ScriptDirectory.from_config(_config(versions)).generate_revision(
        NEW_REVISION, "test", head=PARENT_REVISION, version_path=str(versions), **body
    )
    return _generated(versions, NEW_REVISION).read_text(encoding="utf-8")


def _assert_clean(tmp_path: Path, source: str) -> None:
    """The output compiles, black leaves it unchanged, and ruff passes."""
    compile(source, "<revision>", "exec")
    assert black.format_str(source, mode=black.Mode()) == source
    path = tmp_path / "rendered_revision.py"
    path.write_text(source, encoding="utf-8")
    # The file lives outside backend/, where `alembic` is a first-party package
    # (backend/alembic/); declare it so isort groups the imports as in versions/.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--config",
            str(BACKEND / "pyproject.toml"),
            "--config",
            'lint.isort.known-first-party = ["alembic"]',
            str(path),
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_template_renders_upgrade_and_downgrade_stubs(tmp_path):
    source = _render(tmp_path)

    assert "def upgrade() -> None:" in source
    assert "def downgrade() -> None:" in source
    assert f'revision = "{NEW_REVISION}"' in source
    assert f'down_revision = "{PARENT_REVISION}"' in source
    assert "branch_labels = None" in source
    assert "depends_on = None" in source
    assert "    pass" in source
    assert "import" not in source


def test_empty_revision_is_black_and_ruff_clean(tmp_path):
    source = _render(tmp_path)

    assert '"""\n\n# revision identifiers' in source
    _assert_clean(tmp_path, source)


def test_op_and_sa_body_with_imports_is_clean(tmp_path):
    # Fails on a template that does not emit `import sqlalchemy as sa` (F821).
    source = _render(
        tmp_path,
        imports="from sqlalchemy.dialects import postgresql",
        upgrades=(
            'op.add_column("users", sa.Column("prefs", postgresql.JSONB(), '
            "nullable=True))"
        ),
        downgrades='op.drop_column("users", "prefs")',
    )

    assert (
        "import sqlalchemy as sa\n"
        "from sqlalchemy.dialects import postgresql\n"
        "\n"
        "from alembic import op\n"
    ) in source
    assert source.count("import sqlalchemy as sa") == 1
    assert '"""\n\nimport sqlalchemy as sa\n' in source
    _assert_clean(tmp_path, source)


def test_op_only_body_imports_only_op(tmp_path):
    source = _render(
        tmp_path,
        upgrades='op.execute("UPDATE users SET default_tab = NULL")',
        downgrades="pass",
    )

    assert '"""\n\nfrom alembic import op\n\n# revision identifiers' in source
    assert "import sqlalchemy" not in source
    _assert_clean(tmp_path, source)


def test_merge_revision_has_tuple_down_revision(tmp_path):
    versions = tmp_path / "versions"
    _write_revision(versions, PARENT_REVISION, None)
    _write_revision(versions, "left0000", PARENT_REVISION)
    _write_revision(versions, "right000", PARENT_REVISION)
    cfg = _config(versions)

    command.merge(cfg, ["left0000", "right000"], message="merge", rev_id=NEW_REVISION)
    source = _generated(versions, NEW_REVISION).read_text(encoding="utf-8")

    assert 'down_revision = ("left0000", "right000")' in source
    _assert_clean(tmp_path, source)


def test_first_revision_has_none_down_revision(tmp_path):
    versions = tmp_path / "versions"
    versions.mkdir()
    ScriptDirectory.from_config(_config(versions)).generate_revision(
        NEW_REVISION, "first", head="base", version_path=str(versions)
    )
    source = _generated(versions, NEW_REVISION).read_text(encoding="utf-8")

    assert "down_revision = None" in source
    _assert_clean(tmp_path, source)

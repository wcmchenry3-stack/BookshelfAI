"""The Alembic revision template renders a valid, lint-clean migration (#843).

``alembic revision`` (and ``--autogenerate``, which docs/data-model.md tells
developers to run) needs ``alembic/script.py.mako``. Without it the command
fails with FileNotFoundError. These tests render a revision through Alembic's
own command API into a temporary versions directory, so the real
``alembic/versions/`` is never touched and no database is needed.
"""

import subprocess
import sys
from pathlib import Path

import black
from alembic.config import Config

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


def _render(tmp_path: Path) -> str:
    """Write a parent revision, then render a new empty one; return its source."""
    versions = tmp_path / "versions"
    versions.mkdir()
    (versions / "base0000_base.py").write_text(
        f'revision = "{PARENT_REVISION}"\ndown_revision = None\n'
        "branch_labels = None\ndepends_on = None\n\n\n"
        "def upgrade() -> None:\n    pass\n\n\n"
        "def downgrade() -> None:\n    pass\n",
        encoding="utf-8",
    )
    command.revision(_config(versions), message="test", rev_id=NEW_REVISION)
    generated = list(versions.glob(f"{NEW_REVISION}_*.py"))
    assert len(generated) == 1, generated
    return generated[0].read_text(encoding="utf-8")


def test_template_renders_upgrade_and_downgrade_stubs(tmp_path):
    source = _render(tmp_path)

    assert "def upgrade() -> None:" in source
    assert "def downgrade() -> None:" in source
    assert f'revision = "{NEW_REVISION}"' in source
    assert f'down_revision = "{PARENT_REVISION}"' in source
    assert "branch_labels = None" in source
    assert "depends_on = None" in source
    assert "    pass" in source


def test_rendered_revision_is_black_clean(tmp_path):
    source = _render(tmp_path)

    assert black.format_str(source, mode=black.Mode()) == source


def test_rendered_revision_passes_ruff(tmp_path):
    _render(tmp_path)
    generated = next((tmp_path / "versions").glob(f"{NEW_REVISION}_*.py"))

    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--no-cache", str(generated)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

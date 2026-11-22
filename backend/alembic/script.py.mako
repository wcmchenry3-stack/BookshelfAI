<%!
def _lit(value):
    # Double quotes, as the rest of backend/alembic/versions/ and black write them.
    if value is None:
        return "None"
    if isinstance(value, (tuple, list)):
        return "(" + ", ".join(f'"{v}"' for v in value) + ("," if len(value) == 1 else "") + ")"
    return f'"{value}"'

def _has_ops(body):
    # Alembic's stub for an empty autogenerate is comments plus `pass`; only real
    # operations need `op`, and an unused import fails ruff (F401).
    lines = [line.strip() for line in (body or "").splitlines()]
    return any(line and not line.startswith("#") and line != "pass" for line in lines)
%>"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
% if imports:
${imports}

% endif
% if _has_ops(upgrades) or _has_ops(downgrades):
from alembic import op
% endif

# revision identifiers, used by Alembic.
revision = ${_lit(up_revision)}
down_revision = ${_lit(down_revision)}
branch_labels = ${_lit(branch_labels)}
depends_on = ${_lit(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}

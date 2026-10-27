"""Add users.token_version for access-token revocation

Every access and refresh token carries the user's token_version as the ``tv``
claim; ``get_current_user`` compares it with this column on every request, so
bumping it (app/auth/sessions.py: revoke_all_sessions) revokes every token the
user holds (#689).

Additive (expand only): a NOT NULL integer with server default 0, so existing
rows get 0 and code that does not know the column keeps inserting users.
Downgrade drops the column.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-27
"""

import sqlalchemy as sa

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "token_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "token_version")

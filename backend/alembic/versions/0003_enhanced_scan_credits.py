"""enhanced scan credits on users

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-25
"""

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "enhanced_scan_credits",
            sa.Integer(),
            nullable=False,
            server_default="5",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "enhanced_scan_credits")

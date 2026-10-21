"""scan_usage_daily table for per-user and global daily scan caps

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-21
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scan_usage_daily",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("scans", sa.Integer(), nullable=False, server_default="0"),
        # The composite primary key is also the unique (user_id, usage_date)
        # constraint the atomic reservation upsert targets.
        sa.PrimaryKeyConstraint("user_id", "usage_date"),
    )
    op.create_index(
        "ix_scan_usage_daily_usage_date", "scan_usage_daily", ["usage_date"]
    )


def downgrade() -> None:
    op.drop_index("ix_scan_usage_daily_usage_date", table_name="scan_usage_daily")
    op.drop_table("scan_usage_daily")

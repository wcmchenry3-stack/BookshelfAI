"""Contract step for #528/#533: drop the legacy scan counters (#540)

Contract (destructive) step. The expand steps shipped earlier: #528 moved the
daily caps to ``ai_quota`` and stopped reading and writing ``scan_usage_daily``;
#533 stopped reading and writing ``users.enhanced_scan_credits``. It:

* drops the table ``scan_usage_daily`` (and its index
  ``ix_scan_usage_daily_usage_date``), the per-user per-day scan counter that
  ``app/services/scan_quota.py`` kept; every row is deleted with it;
* drops the column ``users.enhanced_scan_credits`` (the old lifetime
  enhanced-scan credit balance); every user's value is deleted with it.

Deploy order: this runs as the ``preDeployCommand`` while the previous release
still serves. That release maps ``users.enhanced_scan_credits`` on the ``User``
model, so every query that loads a user names the column and fails until the
new code is live; the window is the length of one deploy. See "Upgrade notes"
in docs/deployment.md.

Downgrade recreates the table and the column with exactly the shapes 0005 and
0003 gave them (schema only: columns, types, nullability, server defaults,
primary key, the foreign key with ``ON DELETE CASCADE`` and the index). It does
NOT restore data: the counter rows and the credit balances are gone, and every
user comes back with the column's server default of 5. No data is needed to
roll back, since nothing reads either one.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-26
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_scan_usage_daily_usage_date", table_name="scan_usage_daily")
    op.drop_table("scan_usage_daily")
    op.drop_column("users", "enhanced_scan_credits")


def downgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "enhanced_scan_credits",
            sa.Integer(),
            nullable=False,
            server_default="5",
        ),
    )
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
        sa.PrimaryKeyConstraint("user_id", "usage_date"),
    )
    op.create_index(
        "ix_scan_usage_daily_usage_date", "scan_usage_daily", ["usage_date"]
    )

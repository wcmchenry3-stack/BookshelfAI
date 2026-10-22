"""ai_quota and ai_inflight tables for per-user scan allowances and the
one-call-in-flight lease (#528)

Additive only (expand). The downgrade drops both tables, which only hold
counters and short-lived leases, no user content.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-22
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_quota",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("feature", sa.String(length=64), nullable=False),
        sa.Column("window_kind", sa.String(length=8), nullable=False),
        sa.Column("window_start", sa.Date(), nullable=False),
        sa.Column("units_used", sa.Integer(), nullable=False, server_default="0"),
        # The composite primary key is also the unique key the reservation
        # upsert targets.
        sa.PrimaryKeyConstraint("user_id", "feature", "window_kind", "window_start"),
        sa.CheckConstraint(
            "window_kind IN ('day', 'month')", name="ck_ai_quota_window_kind"
        ),
        sa.CheckConstraint("units_used >= 0", name="ck_ai_quota_units_used"),
    )
    op.create_index("ix_ai_quota_window", "ai_quota", ["window_kind", "window_start"])
    op.create_table(
        "ai_inflight",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("lease_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("ai_inflight")
    op.drop_index("ix_ai_quota_window", table_name="ai_quota")
    op.drop_table("ai_quota")

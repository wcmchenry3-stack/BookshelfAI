"""ai_enhanced_zero_results: uncharged enhanced scans that found no books (#533)

Additive only (expand). One small row per uncharged zero-result enhanced scan
(user, time, ledger call id; no content), kept 30 days. The downgrade drops
the table, which only holds these short-lived abuse counters.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-22
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_enhanced_zero_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ledger_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ai_enhanced_zero_results_user_created",
        "ai_enhanced_zero_results",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ai_enhanced_zero_results_user_created",
        table_name="ai_enhanced_zero_results",
    )
    op.drop_table("ai_enhanced_zero_results")

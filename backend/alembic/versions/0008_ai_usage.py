"""ai_usage ledger and ai_usage_daily rollup (#530)

Additive only (expand). ``ai_usage`` holds one row per paid AI provider call
(counts and cost, no content); ``ai_usage_daily`` holds the daily totals that
raw rows are rolled up into after 30 days. The downgrade drops both tables,
which hold cost accounting only, no user content.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-22
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

# Frozen copy of app.models.ai_usage.USAGE_STATUSES at this revision.
_STATUS_IN = (
    "status IN ('ok', 'refused', 'truncated', 'error', 'timeout', "
    "'refunded', 'cache_hit')"
)


def upgrade() -> None:
    op.create_table(
        "ai_usage",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("call_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("feature", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("price_version", sa.String(length=16), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_micros", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        # Makes the ledger insert idempotent (ON CONFLICT (call_id) DO NOTHING).
        sa.UniqueConstraint("call_id", name="uq_ai_usage_call_id"),
        sa.CheckConstraint(_STATUS_IN, name="ck_ai_usage_status"),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND cache_read_tokens >= 0 "
            "AND cache_write_tokens >= 0 AND cost_micros >= 0",
            name="ck_ai_usage_non_negative",
        ),
    )
    op.create_index("ix_ai_usage_created_at", "ai_usage", ["created_at"])
    op.create_index("ix_ai_usage_user_created", "ai_usage", ["user_id", "created_at"])
    op.create_table(
        "ai_usage_daily",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("feature", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("calls", sa.BigInteger(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cost_micros", sa.BigInteger(), nullable=False),
        # The upsert target.
        sa.PrimaryKeyConstraint("day", "feature", "model", "status"),
        sa.CheckConstraint(_STATUS_IN, name="ck_ai_usage_daily_status"),
        sa.CheckConstraint(
            "calls >= 0 AND input_tokens >= 0 AND output_tokens >= 0 "
            "AND cost_micros >= 0",
            name="ck_ai_usage_daily_non_negative",
        ),
    )


def downgrade() -> None:
    op.drop_table("ai_usage_daily")
    op.drop_index("ix_ai_usage_user_created", table_name="ai_usage")
    op.drop_index("ix_ai_usage_created_at", table_name="ai_usage")
    op.drop_table("ai_usage")

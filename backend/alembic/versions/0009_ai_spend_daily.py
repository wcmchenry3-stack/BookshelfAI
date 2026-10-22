"""ai_spend_daily: the service-wide daily AI cost circuit breaker (#531)

Additive only (expand). One row per UTC day with the reserved and settled
spend in micro-dollars and the alert bits; no user data. The downgrade drops
the table, which holds cost accounting only.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-22
"""

import sqlalchemy as sa

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_spend_daily",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column(
            "reserved_micros", sa.BigInteger(), server_default="0", nullable=False
        ),
        sa.Column(
            "settled_micros", sa.BigInteger(), server_default="0", nullable=False
        ),
        sa.Column(
            "alerted_mask", sa.SmallInteger(), server_default="0", nullable=False
        ),
        sa.PrimaryKeyConstraint("day"),
        sa.CheckConstraint(
            "reserved_micros >= 0 AND settled_micros >= 0 AND alerted_mask >= 0",
            name="ck_ai_spend_daily_non_negative",
        ),
    )


def downgrade() -> None:
    op.drop_table("ai_spend_daily")

"""scan_records: one record per user and photo fingerprint (#585)

Additive only (expand). One row per (user, SHA-256 of the normalised image):
the provider's validated candidate list, the enrichment result (Open Library /
Google Books), which of those steps finished (``ai_done``, ``enrich_done``)
and the feature, tier and model that produced it. Never the image bytes and
never the model's raw output. Rows are deleted ``scan_records.retention_days``
(default 30) after ``created_at`` and go with the user (ON DELETE CASCADE).
The ``ai_usage`` status check already allows ``cache_hit`` (0008), which a
repeat records, so it is unchanged. The downgrade drops the table, which
only holds re-creatable scan results.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-22
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scan_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("feature", sa.String(64), nullable=False),
        sa.Column("tier", sa.String(16), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("candidates", postgresql.JSONB(), nullable=False),
        sa.Column("enrichment", postgresql.JSONB(), nullable=True),
        sa.Column("ai_done", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "enrich_done", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "fingerprint", name="uq_scan_records_user_fingerprint"
        ),
        sa.CheckConstraint(
            "char_length(fingerprint) = 64", name="ck_scan_records_fingerprint"
        ),
        sa.CheckConstraint(
            "NOT enrich_done OR (ai_done AND enrichment IS NOT NULL)",
            name="ck_scan_records_steps",
        ),
    )
    op.create_index(
        "ix_scan_records_user_created", "scan_records", ["user_id", "created_at"]
    )
    op.create_index("ix_scan_records_created_at", "scan_records", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_scan_records_created_at", table_name="scan_records")
    op.drop_index("ix_scan_records_user_created", table_name="scan_records")
    op.drop_table("scan_records")

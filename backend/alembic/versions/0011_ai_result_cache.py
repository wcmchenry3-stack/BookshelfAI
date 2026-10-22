"""ai_result_cache: per-user scan results by normalised-image SHA-256 (#534)

Additive only (expand). One row per (user, image hash, feature, model, prompt
version): the hash and the validated candidate list, never the image or the
model's raw output. Rows expire after 30 days (``cache.ttl_days``) and go with
the user (ON DELETE CASCADE). The ``ai_usage`` status check already allows
``cache_hit`` (0008), so it is unchanged. The downgrade drops the table, which
only holds re-creatable cached results.

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
        "ai_result_cache",
        sa.Column("cache_key", sa.String(64), nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("image_sha256", sa.String(64), nullable=False),
        sa.Column("feature", sa.String(64), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(32), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("cache_key"),
        sa.CheckConstraint(
            "char_length(cache_key) = 64 AND char_length(image_sha256) = 64",
            name="ck_ai_result_cache_hash_length",
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_ai_result_cache_expiry"),
    )
    op.create_index(
        "ix_ai_result_cache_user_created",
        "ai_result_cache",
        ["user_id", "created_at"],
    )
    op.create_index("ix_ai_result_cache_expires_at", "ai_result_cache", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_ai_result_cache_expires_at", table_name="ai_result_cache")
    op.drop_index("ix_ai_result_cache_user_created", table_name="ai_result_cache")
    op.drop_table("ai_result_cache")

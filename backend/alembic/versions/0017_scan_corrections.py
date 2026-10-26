"""scan_corrections: how often users correct a scan's reading (#631)

Expand only: two new tables, nothing existing is changed.

``scan_corrections``: one row per (scan record, candidate) saved as an
"Add it yourself" entry pre-filled from the scan, with one server-derived
boolean each for title, author and ISBN changed. No text column. FK
``scan_records`` ON DELETE CASCADE (the user is reachable through it);
unique (scan_record_id, candidate_index) so a second save counts once.

``scan_corrections_daily``: totals per (UTC day, feature, model), counted
when a correction row is inserted; no user column.

Downgrade drops only these two tables.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-26
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scan_corrections",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "scan_record_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("scan_records.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("candidate_index", sa.SmallInteger(), nullable=False),
        sa.Column("feature", sa.String(length=64), nullable=False),
        sa.Column("tier", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("title_edited", sa.Boolean(), nullable=False),
        sa.Column("author_edited", sa.Boolean(), nullable=False),
        sa.Column("isbn_edited", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scan_record_id",
            "candidate_index",
            name="uq_scan_corrections_record_candidate",
        ),
        sa.CheckConstraint("candidate_index >= 0", name="ck_scan_corrections_index"),
    )
    op.create_index(
        "ix_scan_corrections_created_at", "scan_corrections", ["created_at"]
    )
    op.create_table(
        "scan_corrections_daily",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("feature", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("prefilled_saves", sa.BigInteger(), nullable=False),
        sa.Column("title_edited", sa.BigInteger(), nullable=False),
        sa.Column("author_edited", sa.BigInteger(), nullable=False),
        sa.Column("isbn_edited", sa.BigInteger(), nullable=False),
        sa.Column("any_edited", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("day", "feature", "model"),
        sa.CheckConstraint(
            "prefilled_saves >= 0 AND title_edited >= 0 AND author_edited >= 0 "
            "AND isbn_edited >= 0 AND any_edited >= 0",
            name="ck_scan_corrections_daily_non_negative",
        ),
    )


def downgrade() -> None:
    op.drop_table("scan_corrections_daily")
    op.drop_index("ix_scan_corrections_created_at", table_name="scan_corrections")
    op.drop_table("scan_corrections")

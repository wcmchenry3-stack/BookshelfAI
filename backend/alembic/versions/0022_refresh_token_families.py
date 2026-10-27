"""Refresh-token families: family_id, used_at, replaced_by_id

Rotation chains for reuse detection (#690). Every sign-in starts a family
(the first row's id); each rotation's successor inherits it, and the rotated
row records when (``used_at``) and by what (``replaced_by_id``, a self-FK,
ON DELETE SET NULL, indexed so the cleanup's deletes stay cheap) it was
replaced. See app/auth/refresh_family.py.

Additive (expand only): three nullable columns, one foreign key and one index.
``family_id`` stays nullable so code from before this migration can keep
inserting rows during the deploy; the app treats a NULL family as the row's
own id. A later contract migration may make it NOT NULL.

Backfill: every existing row becomes its own family (``family_id = id``), in
batches of 5,000 rows so no single UPDATE touches the whole table. Written
with SQLAlchemy Core (no raw SQL). Existing rotated rows get no ``used_at``,
so they count as plainly revoked (no reuse detection for rotations made
before this release).

Downgrade drops the index, the foreign key and the three columns.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-27
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

BACKFILL_BATCH_SIZE = 5_000

_FK = "refresh_tokens_replaced_by_id_fkey"
_INDEX = "ix_refresh_tokens_replaced_by_id"


def backfill_family_ids(connection, batch_size: int = BACKFILL_BATCH_SIZE) -> int:
    """Set ``family_id = id`` on rows without one, ``batch_size`` rows per
    UPDATE, until none are left. Returns how many rows were updated."""
    tokens = sa.table(
        "refresh_tokens",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("family_id", postgresql.UUID(as_uuid=True)),
    )
    total = 0
    while True:
        batch = (
            sa.select(tokens.c.id)
            .where(tokens.c.family_id.is_(None))
            .limit(batch_size)
            .scalar_subquery()
        )
        updated = connection.execute(
            sa.update(tokens)
            .where(tokens.c.id.in_(batch))
            .values(family_id=tokens.c.id)
        ).rowcount
        total += updated
        if updated < batch_size:
            return total


def upgrade() -> None:
    op.add_column(
        "refresh_tokens",
        sa.Column("family_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "refresh_tokens",
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "refresh_tokens",
        sa.Column("replaced_by_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        _FK,
        "refresh_tokens",
        "refresh_tokens",
        ["replaced_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(_INDEX, "refresh_tokens", ["replaced_by_id"])
    backfill_family_ids(op.get_bind(), BACKFILL_BATCH_SIZE)


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="refresh_tokens")
    op.drop_constraint(_FK, "refresh_tokens", type_="foreignkey")
    op.drop_column("refresh_tokens", "replaced_by_id")
    op.drop_column("refresh_tokens", "used_at")
    op.drop_column("refresh_tokens", "family_id")

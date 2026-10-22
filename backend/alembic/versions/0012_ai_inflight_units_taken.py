"""ai_inflight: which scan units the lease holder took (#590)

Additive only (expand). Three booleans on the per-user lease row, server
default false: ``daily_taken``, ``monthly_taken`` and ``enhanced_taken``. Each
is set in the same transaction as its ``ai_quota`` increment, so after a
commit whose acknowledgement was lost the row shows whether the unit landed,
and each give-back clears its flag in the same transaction as its decrement
(at most once). No new rows: still at most one row per user. The downgrade
drops the columns, which only describe leases in flight (seconds long).

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-22
"""

import sqlalchemy as sa

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

COLUMNS = ("daily_taken", "monthly_taken", "enhanced_taken")


def upgrade() -> None:
    for name in COLUMNS:
        op.add_column(
            "ai_inflight",
            sa.Column(name, sa.Boolean(), server_default=sa.false(), nullable=False),
        )


def downgrade() -> None:
    for name in reversed(COLUMNS):
        op.drop_column("ai_inflight", name)

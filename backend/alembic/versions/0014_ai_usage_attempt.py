"""ai_usage.attempt: which try of a /scan request a ledger row belongs to (#603)

Additive only (expand). One nullable ``SMALLINT`` column on ``ai_usage``,
no default: existing rows stay ``NULL`` (unknown), and /scan sets it on
every row it writes from the request's ``attempt`` form field (1 the first
try, 2..6 the app's scheduled retries). Purely informational: never used for
charging, limits or ordering. ``ai_usage_daily`` is unchanged (the rollup
does not carry it; the 30 days of raw rows are enough to count retries).

Locking: adding a nullable column with no default only updates the catalog
(no table rewrite, no scan), under a brief ACCESS EXCLUSIVE lock; a 5 s
``lock_timeout`` makes it fail fast instead of queueing every query behind a
long transaction.

The downgrade drops the column (the per-request detail is lost; nothing
else reads it).

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-23
"""

import sqlalchemy as sa

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

_LOCK_TIMEOUT = "5s"


def upgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
    op.add_column("ai_usage", sa.Column("attempt", sa.SmallInteger(), nullable=True))


def downgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
    op.drop_column("ai_usage", "attempt")

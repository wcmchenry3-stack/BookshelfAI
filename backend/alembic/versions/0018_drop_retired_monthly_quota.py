"""Contract step for #582: drop the retired monthly quota keys and flags (#594)

Contract (destructive) step; the expand step shipped with #582, which stopped
reading and writing all of this. It:

* deletes the three retired ``ai_settings`` rows ``quota.free.monthly``,
  ``quota.premium.monthly`` and ``quota.premium.enhanced_monthly`` (no other
  key is touched);
* drops ``ai_inflight.monthly_taken`` and ``ai_inflight.enhanced_taken`` (added
  by 0012; they only describe leases in flight, seconds long).

``ai_quota`` month rows are deliberately left alone: nothing reads or writes
them and the daily cleanup task purges them after 13 months.

Deploy order: this runs as the ``preDeployCommand`` while the previous
release (#582 code) still serves. That code names the two columns in the
lease acquisition statement (the takeover reset), so until the new code is
live every lease acquisition fails and scans are refused; the window is the
length of one deploy. The release that ships this must be the one after #582.

Downgrade recreates the two columns exactly as 0012 made them (boolean, not
null, server default false). It does NOT restore the deleted ``ai_settings``
rows: their operator-set values are gone. None is needed to roll back, since
the release before #582 seeds any missing quota key from its env defaults at
boot (``INSERT ... ON CONFLICT DO NOTHING``).

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-26
"""

import sqlalchemy as sa

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

RETIRED_KEYS = (
    "quota.free.monthly",
    "quota.premium.monthly",
    "quota.premium.enhanced_monthly",
)
COLUMNS = ("monthly_taken", "enhanced_taken")

ai_settings = sa.table("ai_settings", sa.column("key", sa.Text()))


def upgrade() -> None:
    op.execute(sa.delete(ai_settings).where(ai_settings.c.key.in_(RETIRED_KEYS)))
    for name in COLUMNS:
        op.drop_column("ai_inflight", name)


def downgrade() -> None:
    for name in COLUMNS:
        op.add_column(
            "ai_inflight",
            sa.Column(name, sa.Boolean(), server_default=sa.false(), nullable=False),
        )

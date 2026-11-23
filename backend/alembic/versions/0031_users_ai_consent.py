"""Add users.ai_consent_at and users.ai_consent_withdrawn_at (#1023)

Consent to send scan photos to the AI provider (Apple 5.1.2(i)), recorded per
account: ``ai_consent_at`` is when it was last given and
``ai_consent_withdrawn_at`` when it was last withdrawn (both server time).
Consent stands when it was given and not withdrawn since.

Expand only: two nullable ``timestamptz`` columns with no default, so existing
users have no consent (they are asked at their next scan) and no backfill is
needed. The previous release, still serving while this runs, never reads or
writes them and inserts users without them (NULL).

Locking: adding a nullable column with no default only updates the catalog
(no table rewrite or scan), but it needs a brief ACCESS EXCLUSIVE lock on
``users``; a 5 s ``lock_timeout`` makes it fail fast instead of queueing every
query behind a long transaction. Re-running after a failure is safe: each
column is added only when missing.

Downgrade drops both columns; every recorded consent is lost, so the app asks
again (and with ``AI_CONSENT_REQUIRED`` on, scans are refused until then).

Revision ID: 0031
Revises: 0030
Create Date: 2026-11-22
"""

import sqlalchemy as sa

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

_COLUMNS = ("ai_consent_at", "ai_consent_withdrawn_at")
_LOCK_TIMEOUT = "5s"


def upgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("users")}
    op.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
    for name in _COLUMNS:
        if name not in existing:
            op.add_column(
                "users",
                sa.Column(name, sa.DateTime(timezone=True), nullable=True),
            )


def downgrade() -> None:
    for name in reversed(_COLUMNS):
        op.drop_column("users", name)

"""ai_usage status ``malformed``: a billed reply with no usable answer (#572)

Additive only (expand). Widens the status CHECK of ``ai_usage`` and
``ai_usage_daily`` to allow ``malformed`` (the provider answered HTTP 200 but
the reply was missing, not JSON, of the wrong shape, or ended on an
unexpected stop reason). No column, row or index changes; both tables hold
cost accounting only, no user content.

The downgrade restores the previous CHECK as ``NOT VALID``: rows already
written as ``malformed`` are kept (deleting or relabelling cost history would
lose data), while new rows must use the old statuses again, as the old code
does.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-22
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

# Frozen copies of app.models.ai_usage.USAGE_STATUSES before and at this
# revision.
_OLD_STATUSES = (
    "'ok', 'refused', 'truncated', 'error', 'timeout', 'refunded', 'cache_hit'"
)
_NEW_STATUSES = _OLD_STATUSES + ", 'malformed'"

_CHECKS = (
    ("ai_usage", "ck_ai_usage_status"),
    ("ai_usage_daily", "ck_ai_usage_daily_status"),
)


def upgrade() -> None:
    for table, name in _CHECKS:
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, f"status IN ({_NEW_STATUSES})")


def downgrade() -> None:
    for table, name in _CHECKS:
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(
            name,
            table,
            f"status IN ({_OLD_STATUSES})",
            postgresql_not_valid=True,
        )

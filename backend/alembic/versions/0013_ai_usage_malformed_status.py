"""ai_usage status ``malformed``: a billed reply with no usable answer (#572)

Additive only (expand). Widens the status CHECK of ``ai_usage`` and
``ai_usage_daily`` to allow ``malformed`` (the provider answered HTTP 200 but
the reply was missing, not JSON, of the wrong shape, ended on an unexpected
stop reason, or was cut off before a single complete book). No column, row
or index changes; both tables hold cost accounting only, no user content.

Locking: the swap (drop the old CHECK, add the widened one ``NOT VALID``)
takes an ACCESS EXCLUSIVE lock but scans nothing, under a 5 s
``lock_timeout`` so it fails fast instead of queueing every query behind a
long transaction. The swap is then committed and each CHECK is validated in
its own transaction (``VALIDATE CONSTRAINT``, a SHARE UPDATE EXCLUSIVE lock
that does not block reads or writes), so no ACCESS EXCLUSIVE lock is held
while the rows are scanned. If a validation fails, the widened CHECK stays
``NOT VALID`` (still enforced on new rows) and the revision is not stamped;
running the upgrade again redoes both steps.

The downgrade restores the previous CHECK on ``ai_usage`` only, as
``NOT VALID``: rows already written as ``malformed`` are kept (deleting or
relabelling cost history would lose data), and the old code never writes
``malformed``. ``ai_usage_daily`` keeps the widened CHECK: the rollup
(``ledger.rollup_and_purge``) copies those kept raw rows into it, and the old
CHECK there would make every rollup batch fail.

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

_LOCK_TIMEOUT = "5s"


def upgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
    for table, name in _CHECKS:
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(
            name, table, f"status IN ({_NEW_STATUSES})", postgresql_not_valid=True
        )
    # Commits the swap; each statement below is its own transaction.
    with op.get_context().autocommit_block():
        op.execute(f"SET lock_timeout = '{_LOCK_TIMEOUT}'")
        try:
            for table, name in _CHECKS:
                op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")
        finally:
            op.execute("RESET lock_timeout")


def downgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
    table, name = _CHECKS[0]
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(
        name, table, f"status IN ({_OLD_STATUSES})", postgresql_not_valid=True
    )

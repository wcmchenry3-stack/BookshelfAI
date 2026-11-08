"""Add users.default_tab for the "Open on" preference (#913)

The screen the app opens on after sign-in and on launch: ``scan``,
``wishlist`` or ``my-books``. NULL means the default, Scan, so no backfill is
needed and every existing user keeps opening on Scan.

Expand only: a nullable ``varchar`` plus the check constraint
``ck_users_default_tab`` (NULL or one of the three values). The previous
release, still serving while this runs, never reads or writes the column and
inserts users without it (NULL), which the constraint allows.

Locking, as in 0013: adding the column and the CHECK ``NOT VALID`` takes a
brief ACCESS EXCLUSIVE lock on ``users`` and scans nothing, under a 5 s
``lock_timeout`` so it fails fast instead of queueing every query behind a
long transaction. That is committed, then the CHECK is validated in its own
transaction (``VALIDATE CONSTRAINT``, a SHARE UPDATE EXCLUSIVE lock that does
not block reads or writes), so no ACCESS EXCLUSIVE lock is held while the
rows are scanned. If the validation fails, the CHECK stays ``NOT VALID``
(still enforced on new rows) and the revision is not stamped; running the
upgrade again redoes both steps.

Downgrade drops the constraint and the column; users' "Open on" choices are
lost, and the app falls back to Scan.

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-07
"""

import sqlalchemy as sa

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

# Kept literal (not imported from app.models.user) so the migration stays
# what it was when written, whatever the model later says.
_DEFAULT_TABS = ("scan", "wishlist", "my-books")


_LOCK_TIMEOUT = "5s"


def upgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
    op.add_column("users", sa.Column("default_tab", sa.String(), nullable=True))
    op.create_check_constraint(
        "ck_users_default_tab",
        "users",
        f"default_tab IS NULL OR default_tab IN {_DEFAULT_TABS}",
        postgresql_not_valid=True,
    )
    # Commits the step above; each statement below is its own transaction.
    with op.get_context().autocommit_block():
        op.execute(f"SET lock_timeout = '{_LOCK_TIMEOUT}'")
        try:
            op.execute("ALTER TABLE users VALIDATE CONSTRAINT ck_users_default_tab")
        finally:
            op.execute("RESET lock_timeout")


def downgrade() -> None:
    op.drop_constraint("ck_users_default_tab", "users", type_="check")
    op.drop_column("users", "default_tab")

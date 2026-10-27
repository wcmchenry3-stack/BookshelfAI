"""Unique index on lower(users.email) (#705)

``users.email`` was unique only case-sensitively, so two concurrent first
sign-ins from different Google subjects whose emails differ only in case
(``Me@x.com`` and ``me@x.com``) could both commit: two rows for one mailbox.
The lookups in ``app/auth/identity.py`` already compare ``lower(email)``
(#702); this revision adds ``uq_users_email_lower``, a unique index on
``users (lower(email))``, so the losing insert hits a unique violation
(SQLSTATE 23505). ``resolve_and_commit_google_user`` rolls back, retries the
lookup, finds the winner's row and answers ``409 account_conflict``.

Expand only. The existing case-sensitive unique constraint
(``users_email_key``) is kept: it is implied by the new index, so it can never
fail on its own, and dropping it would be a separate contract step for no
gain (the extra index on a small table costs next to nothing).

Existing case-duplicates are not merged: which row is the real account is an
operator decision (see ``RENDER.md`` on resolving ``account_conflict``). If
any exist, the upgrade fails with the number of affected addresses only (never
an email), before anything is created. ``users`` is locked against writes
(``SHARE`` mode, 5 s ``lock_timeout``) before the check, so no duplicate can
be inserted between the check and the index build; otherwise the build would
fail with Postgres' own message, which includes the duplicate key, i.e. an
email address. ``users`` is small, so a plain (not ``CONCURRENTLY``) build
holds the lock for milliseconds. ``lock_timeout`` is reset to the default at
the end of the revision.

Offline mode (``alembic upgrade --sql``) is refused: the pre-check reads rows.

Downgrade drops the index only.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-27
"""

import sqlalchemy as sa

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

INDEX = "uq_users_email_lower"
_LOCK_TIMEOUT = "5s"

_users = sa.table("users", sa.column("email", sa.String))


def count_case_duplicates(connection) -> int:
    """Number of lower(email) values held by more than one row."""
    groups = (
        sa.select(sa.func.lower(_users.c.email).label("e"))
        .group_by(sa.func.lower(_users.c.email))
        .having(sa.func.count() > 1)
        .subquery()
    )
    return connection.execute(
        sa.select(sa.func.count()).select_from(groups)
    ).scalar_one()


def upgrade() -> None:
    # The same check as context.is_offline_mode(), but it also works when the
    # revision is run through Operations directly (the migration test).
    if op.get_context().as_sql:
        raise RuntimeError(
            "0022 does not support offline SQL generation (alembic --sql): "
            "its duplicate pre-check reads rows; run it online"
        )
    op.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
    # Blocks writes (not reads) to users until the run commits, so the
    # pre-check below stays true until the index exists.
    op.execute("LOCK TABLE users IN SHARE MODE")
    duplicates = count_case_duplicates(op.get_bind())
    if duplicates:
        raise RuntimeError(
            f"0022: {duplicates} email address(es) are held by more than one "
            "users row, differing only in case. Resolve them by hand (see "
            "RENDER.md, 'Resolving a sign-in account_conflict'), then rerun "
            "the upgrade. Nothing was changed."
        )
    op.create_index(INDEX, "users", [sa.text("lower(email)")], unique=True)
    op.execute("SET LOCAL lock_timeout = DEFAULT")


def downgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
    op.drop_index(INDEX, table_name="users")
    op.execute("SET LOCAL lock_timeout = DEFAULT")

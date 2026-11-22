"""Grant DML privileges on all app tables / sequences to bookshelf_app (#696)

The application connects as ``postgres`` (the table owner) today. Issue #696
introduces a least-privilege ``bookshelf_app`` role: it can only run
SELECT / INSERT / UPDATE / DELETE on app tables and USAGE on sequences. DDL
(DROP TABLE, ALTER TABLE, CREATE TABLE, …) stays with the owner role, which
the app role cannot perform.

What this migration does
------------------------
1. Pre-flight: if ``bookshelf_app`` doesn't exist, raise an informative error
   so the operator knows to create it first (see PR description).

2. Grant ``SELECT, INSERT, UPDATE, DELETE`` on every current app table and
   ``USAGE`` on every sequence in the ``public`` schema to ``bookshelf_app``.

3. Grant ``BYPASSRLS`` to ``bookshelf_app``. RLS is enabled on every table
   (migration 0020). Rather than maintaining per-table policies, we grant
   BYPASSRLS so the app role works exactly as the owner does: full row
   visibility, policy-free. DDL operations (the ones bookshelf_app cannot run)
   are already restricted separately.

4. Set default privileges on the **owner role** (``postgres`` on Supabase,
   whatever role runs this migration elsewhere) so every *future* table and
   sequence created by the owner automatically grants the same privileges to
   ``bookshelf_app``.

Why ``op.execute()`` here
--------------------------
GRANT / ALTER DEFAULT PRIVILEGES are DDL/DCL statements, not DML. They cannot
be expressed with the SQLAlchemy ORM; ``op.execute()`` is the correct tool for
migration-only DDL, per the project's own conventions (see migration 0023 for
precedent).

Down migration
--------------
Revokes all grants and default privileges. Does not drop the role.

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-02
"""

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

# The least-privilege application role.
_APP_ROLE = "bookshelf_app"

# Every table in the public schema at the time of this migration.
# Grants on tables added after 0028 are covered by the DEFAULT PRIVILEGES set
# below, so this list does not need to grow with new migrations.
_TABLES = (
    "ai_enhanced_zero_results",
    "ai_inflight",
    "ai_quota",
    "ai_settings",
    "ai_spend_daily",
    "ai_usage",
    "ai_usage_daily",
    "books",
    "device_keys",
    "editions",
    "refresh_tokens",
    "scan_corrections",
    "scan_corrections_daily",
    "scan_records",
    "user_books",
    "user_custom_books",
    "users",
)


def upgrade() -> None:
    # Pre-flight: the role must exist before the grants can be issued.  The
    # owner creates it by hand in the Supabase SQL editor; see the PR
    # description for the exact CREATE ROLE statement.
    op.execute(f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_APP_ROLE}') THEN
                RAISE EXCEPTION
                    'Role "{_APP_ROLE}" does not exist. Create it first: '
                    'CREATE ROLE {_APP_ROLE} LOGIN PASSWORD ''<strong password>''; '
                    'Then re-run alembic upgrade head.';
            END IF;
        END
        $$
        """)

    # Table grants: DML only, no DDL.
    for table in _TABLES:
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.{table} TO {_APP_ROLE}"
        )

    # Sequence grants: USAGE lets the role call nextval / currval.
    op.execute(f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO {_APP_ROLE}")

    # BYPASSRLS: the app role behaves like the owner for row visibility.
    # RLS (0020) is still on; BYPASSRLS is simpler than per-table policies.
    op.execute(f"ALTER ROLE {_APP_ROLE} BYPASSRLS")

    # Default privileges: tables and sequences created later by the running
    # role (the owner, typically ``postgres``) inherit the same grants.
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {_APP_ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT USAGE ON SEQUENCES TO {_APP_ROLE}"
    )


def downgrade() -> None:
    # Skip silently when the role doesn't exist (e.g. a CI rollback).
    op.execute(f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_APP_ROLE}') THEN
                RAISE NOTICE 'Role "{_APP_ROLE}" not found; downgrade is a no-op.';
                RETURN;
            END IF;

            -- Revoke default privileges.
            ALTER DEFAULT PRIVILEGES IN SCHEMA public
                REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {_APP_ROLE};
            ALTER DEFAULT PRIVILEGES IN SCHEMA public
                REVOKE USAGE ON SEQUENCES FROM {_APP_ROLE};

            -- Revoke BYPASSRLS.
            ALTER ROLE {_APP_ROLE} NOBYPASSRLS;

            -- Revoke all table and sequence privileges.
            REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {_APP_ROLE};
            REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {_APP_ROLE};
        END
        $$
        """)

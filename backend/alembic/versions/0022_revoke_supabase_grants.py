"""Revoke Supabase's anon/authenticated privileges on public tables and sequences

Defence in depth after 0020 (RLS on, no policies). Supabase grants its Data
API roles ``anon`` and ``authenticated`` up to ``ALL`` (stock Supabase grants
``ALL``; the dev branch had fewer) on every table and sequence in ``public``,
and sets default privileges so future objects get the same. RLS
hides rows from those roles but does not govern every privilege: a role that
holds ``TRUNCATE`` can empty a table regardless of RLS, and ``REFERENCES`` and
``TRIGGER`` are not row level either. Sequences have no RLS at all, so
``authenticated`` could advance ``nextval`` on them. This migration removes
the grants so that, with the Data API switched off and RLS on, a third layer
also says no (#704).

What it does, separately for ``anon`` and for ``authenticated``, and only if
that role exists (Supabase creates both; plain Postgres, CI included, has
neither, and there the migration does nothing):

* ``REVOKE ALL ON ALL TABLES IN SCHEMA public`` and ``ON ALL SEQUENCES``;
* ``ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES`` and
  ``ON SEQUENCES``, so tables and sequences created later by the migrating
  role do not get the grants back;
* a self-check: if the role still holds any privilege on a ``public`` relation
  or in the migrating role's ``public`` default privileges, the migration
  raises and rolls back. A ``REVOKE`` only removes grants made by the role
  running it (or, for an owner, by the owner); a grant made by another
  grantor is silently left in place, so the check is what makes the revoke
  trustworthy.

Default privileges are kept per granting role. ``ALTER DEFAULT PRIVILEGES``
without ``FOR ROLE`` changes those of the role running the migration, which
is ``postgres`` on Supabase and is the role that creates every application
table. Supabase also keeps defaults ``FOR ROLE supabase_admin``; ``postgres``
is not a member of ``supabase_admin`` and cannot change them. They only apply
to objects ``supabase_admin`` creates, which the app never does, but an object
the platform adds to ``public`` would still be granted to ``anon``. RLS (0020,
enforced by tests/integration/test_rls_enabled_db.py for every public table)
still covers rows in that case.

Functions are left alone on purpose. ``EXECUTE`` on functions is granted to
``PUBLIC`` by default, so revoking it from ``anon`` and ``authenticated``
alone changes nothing they can actually do; the app defines no functions in
``public``; and Supabase-managed extensions may own functions there, which
``postgres`` can't revoke on anyway.

Schema ``USAGE`` on ``public`` is left alone too: without table or sequence
privileges it only lets the roles see object names.

The app is unaffected: it connects as ``postgres``, the owner of every table,
and owners keep their privileges whatever is revoked from other roles. The
nightly ``pg_dump`` runs with ``--no-acl``, so it neither needs nor carries
these grants.

Downgrade does nothing (apart from a notice). Re-granting ``ALL`` would be
looser than the state before this migration on the dev branch, where the roles
held only DELETE, TRUNCATE, TRIGGER and MAINTAIN, and it is never wanted. If
Supabase's stock grants are ever needed back, run as ``postgres``, for each of
``anon`` and ``authenticated``::

    GRANT ALL ON ALL TABLES IN SCHEMA public TO anon, authenticated;
    GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO anon, authenticated;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO anon, authenticated;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO anon, authenticated;

No data is touched either way.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-27
"""

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

# Supabase's Data API roles. Hardcoded identifiers, never user input.
ROLES = ("anon", "authenticated")

_REVOKE = (
    "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role}",
    "REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {role}",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM {role}",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM {role}",
)

_VERIFY = """\
IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace,
                  aclexplode(c.relacl) a
           WHERE n.nspname = 'public'
             AND a.grantee = (SELECT oid FROM pg_roles WHERE rolname = '{role}'))
   OR EXISTS (SELECT 1 FROM pg_default_acl d, aclexplode(d.defaclacl) a
              WHERE d.defaclrole = current_user::regrole
                AND d.defaclnamespace = 'public'::regnamespace
                AND a.grantee = (SELECT oid FROM pg_roles WHERE rolname = '{role}'))
THEN
    RAISE EXCEPTION '{role} still holds privileges in public after REVOKE';
END IF"""


def _if_role_exists(role: str, statements: tuple[str, ...]) -> str:
    """A DO block that runs ``statements`` only when ``role`` exists.

    PL/pgSQL resolves role names in utility statements only when it reaches
    them, so the block is valid on a server without the role.
    """
    body = "\n".join(f"        {s.format(role=role)};" for s in (*statements, _VERIFY))
    return (
        "DO $$\n"
        "BEGIN\n"
        f"    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN\n"
        f"{body}\n"
        "    END IF;\n"
        "END\n"
        "$$"
    )


def upgrade() -> None:
    for role in ROLES:
        op.execute(_if_role_exists(role, _REVOKE))


def downgrade() -> None:
    # Grants are not restored: see the module docstring.
    op.execute(
        "DO $$ BEGIN RAISE NOTICE '0022 downgrade: anon/authenticated grants "
        "are not restored; see the migration docstring'; END $$"
    )

"""Library audit log: purge through a SECURITY DEFINER function (#1042)

Takes ``DELETE`` on ``library_events`` away from the app role. Until now
``bookshelf_app`` kept ``DELETE`` (migration 0030) only so the daily retention
job could purge old events; a compromised app connection could therefore
erase the audit trail. The app has one database role (``DATABASE_URL``; the
job sessions in ``app/core/database.py`` share its engine), so the purge moves
into a function that runs as its owner instead of a second job role.

- ``public.purge_library_events(cutoff timestamptz, batch integer) RETURNS
  integer``: deletes at most ``batch`` events created before
  ``LEAST(cutoff, now() - 30 days)``, oldest first, and returns how many.

  * ``SECURITY DEFINER``, owned by the migrating role (the table owner, so
    RLS does not hide rows from it).
  * ``SET search_path = public, pg_temp`` (``pg_catalog`` is still searched
    first) and every table name schema-qualified, so a caller's temporary or
    other-schema ``library_events`` is never the one deleted from.
  * The 30-day floor (:data:`_MIN_AGE`) is enforced inside the function:
    the app role chooses the cutoff, so without it a caller could pass
    ``now()`` and wipe the log. 30 days is well under the 180-day retention
    (``LIBRARY_EVENTS_RETENTION_DAYS``) and leaves a month of recent history
    that no app-side bug or compromise can remove. A NULL cutoff or a batch
    outside 1..:data:`_MAX_BATCH` raises ``invalid_parameter_value``.
  * ``SET lock_timeout = '5s'``: the delete gives up instead of queueing
    behind a long lock. The caller's ``statement_timeout`` (the job value,
    ``JobSessionLocal``) still covers the whole call.

- ``EXECUTE`` revoked from ``PUBLIC`` (and from Supabase's ``anon`` /
  ``authenticated`` when they exist, which Supabase's default privileges
  would otherwise grant, #704), granted to ``bookshelf_app``.
- ``DELETE`` on ``library_events`` revoked from ``bookshelf_app``. The
  ``ON DELETE SET NULL`` of ``user_id`` is unaffected: referential actions run
  as the table owner.

Re-running is safe: ``CREATE OR REPLACE`` and the grants are idempotent.
Locking: ``CREATE FUNCTION`` takes no table lock; ``REVOKE`` takes a brief
one on ``library_events``, bounded by a 5 s ``lock_timeout``.

Release overlap: the previous release deletes directly, so during a deploy
its daily purge (if it runs in that window) fails with a permission error,
which ``token_cleanup`` logs and the next day's run catches up.

The migration guard flags this file: the function body holds a
``DELETE FROM`` (it deletes nothing when the migration runs) and the
migration narrows a privilege. It needs the owner's
``destructive-migration-approved`` label.

Downgrade grants ``DELETE`` back first (so the previous code works) and then
drops the function; both are guarded, so it can be re-run too.

Revision ID: 0035
Revises: 0034
Create Date: 2026-11-23
"""

from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

_APP_ROLE = "bookshelf_app"
_SUPABASE_ROLES = ("anon", "authenticated")
_SIGNATURE = "public.purge_library_events(timestamptz, integer)"
_LOCK_TIMEOUT = "5s"
# Kept literal here (not imported) so the migration stays what it was when
# written. The app's batch (library_events_retention.PURGE_BATCH_SIZE) must
# not exceed _MAX_BATCH.
_MIN_AGE = "30 days"
_MAX_BATCH = 10000

_CREATE_FUNCTION = f"""
CREATE OR REPLACE FUNCTION public.purge_library_events(
    cutoff timestamptz, batch integer
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
SET lock_timeout = '{_LOCK_TIMEOUT}'
AS $fn$
DECLARE
    effective_cutoff timestamptz;
    deleted integer;
BEGIN
    IF cutoff IS NULL OR batch IS NULL OR batch < 1 OR batch > {_MAX_BATCH} THEN
        RAISE EXCEPTION 'purge_library_events: cutoff must be set and batch '
            'between 1 and {_MAX_BATCH}'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    -- The floor: never anything younger than {_MIN_AGE}, whatever the caller
    -- passes (LEAST ignores NULL, hence the explicit check above).
    effective_cutoff := LEAST(cutoff, pg_catalog.now() - interval '{_MIN_AGE}');
    DELETE FROM public.library_events
    WHERE id IN (
        SELECT e.id FROM public.library_events e
        WHERE e.created_at < effective_cutoff
        ORDER BY e.created_at
        LIMIT batch
    )
    AND created_at < effective_cutoff;
    GET DIAGNOSTICS deleted = ROW_COUNT;
    RETURN deleted;
END
$fn$
"""


def _revoke_from_supabase_roles() -> str:
    return "\n".join(f"""
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                REVOKE ALL ON FUNCTION {_SIGNATURE} FROM {role};
            END IF;""" for role in _SUPABASE_ROLES)


def upgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
    op.execute(_CREATE_FUNCTION)
    op.execute(f"REVOKE ALL ON FUNCTION {_SIGNATURE} FROM PUBLIC")
    # The role exists (0028 requires it); guarded anyway so a database
    # without it still migrates.
    op.execute(f"""
        DO $$
        BEGIN
            {_revoke_from_supabase_roles()}
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_APP_ROLE}') THEN
                GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO {_APP_ROLE};
                REVOKE DELETE ON public.library_events FROM {_APP_ROLE};
            END IF;
        END
        $$
        """)


def downgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_APP_ROLE}') THEN
                GRANT DELETE ON public.library_events TO {_APP_ROLE};
            END IF;
        END
        $$
        """)
    op.execute(f"DROP FUNCTION IF EXISTS {_SIGNATURE}")

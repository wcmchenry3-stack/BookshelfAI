-- Re-apply the bookshelf_app grants (#696) to a restored database.
--
-- Run as the owner role (`postgres` on Supabase) AFTER the role exists on the
-- target. Needed because a restore made with `pg_restore --no-acl` drops every
-- grant and default privilege, and `alembic_version` already says 0028, so
-- migration 0028 never runs again on the restored database. See
-- docs/backups.md ("Restoring: the app role") and the DB Restore Drill.
--
-- Same end state as migrations 0028, 0030 and 0035: DML on every table in
-- `public` except `alembic_version` (no UPDATE on `library_events`, #684, and
-- no DELETE on it either once its purge function exists, #1042), EXECUTE on
-- `purge_library_events` for the role only, USAGE on every sequence,
-- BYPASSRLS, and the default privileges of the running (owner) role for later
-- tables and sequences.
-- Idempotent: safe to run more than once. One DO block, so it runs as a
-- single statement from psql, the Supabase SQL editor or a driver.
-- DCL (GRANT / ALTER ROLE / ALTER DEFAULT PRIVILEGES) has no ORM form; this is
-- an operator script, not app code, like the DCL in migration 0028.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bookshelf_app') THEN
        RAISE EXCEPTION 'Role "bookshelf_app" does not exist. Create it first, '
            'as postgres: CREATE ROLE bookshelf_app LOGIN PASSWORD ''<strong password>'';';
    END IF;

    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO bookshelf_app;
    GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO bookshelf_app;
    -- 0028 never granted anything on alembic_version: only migrations use it.
    IF to_regclass('public.alembic_version') IS NOT NULL THEN
        REVOKE ALL ON public.alembic_version FROM bookshelf_app;
    END IF;
    -- 0030: the library audit log is append-only for the app (#684).
    IF to_regclass('public.library_events') IS NOT NULL THEN
        REVOKE UPDATE ON public.library_events FROM bookshelf_app;
    END IF;
    -- 0035: the purge goes through a SECURITY DEFINER function, so the app
    -- has no DELETE (#1042). A --no-acl restore also resets the function's
    -- ACL to the default (EXECUTE for PUBLIC), so set it again. On a backup
    -- older than 0035 the function is missing and DELETE stays.
    IF to_regprocedure('public.purge_library_events(timestamptz, integer)') IS NOT NULL THEN
        REVOKE ALL ON FUNCTION public.purge_library_events(timestamptz, integer) FROM PUBLIC;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            REVOKE ALL ON FUNCTION public.purge_library_events(timestamptz, integer) FROM anon;
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            REVOKE ALL ON FUNCTION public.purge_library_events(timestamptz, integer) FROM authenticated;
        END IF;
        GRANT EXECUTE ON FUNCTION public.purge_library_events(timestamptz, integer) TO bookshelf_app;
        IF to_regclass('public.library_events') IS NOT NULL THEN
            REVOKE DELETE ON public.library_events FROM bookshelf_app;
        END IF;
    END IF;

    ALTER ROLE bookshelf_app BYPASSRLS;

    ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bookshelf_app;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT USAGE ON SEQUENCES TO bookshelf_app;
END
$$;

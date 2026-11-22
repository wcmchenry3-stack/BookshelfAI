-- Re-apply the bookshelf_app grants (#696) to a restored database.
--
-- Run as the owner role (`postgres` on Supabase) AFTER the role exists on the
-- target. Needed because a restore made with `pg_restore --no-acl` drops every
-- grant and default privilege, and `alembic_version` already says 0028, so
-- migration 0028 never runs again on the restored database. See
-- docs/backups.md ("Restoring: the app role") and the DB Restore Drill.
--
-- Same end state as migration 0028: DML on every table in `public` except
-- `alembic_version`, USAGE on every sequence, BYPASSRLS, and the default
-- privileges of the running (owner) role for later tables and sequences.
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

    ALTER ROLE bookshelf_app BYPASSRLS;

    ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bookshelf_app;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT USAGE ON SEQUENCES TO bookshelf_app;
END
$$;

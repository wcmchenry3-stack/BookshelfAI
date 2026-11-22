-- Read-only database size report (#673, G8). Run by hand as any role that can
-- connect, for example in the Supabase SQL editor or `psql -f`. It changes
-- nothing. Output is for the owner: do not paste it into a public issue or PR
-- (public repository). Documented in docs/RENDER.md ("Database size and
-- limits") and docs/storage-and-retention.md ("Expected growth").

-- 1. Whole-database size against the plan's disk (Supabase Pro: 8 GB included).
--    pg_database_size excludes WAL and system files, so disk use is a bit higher.
SELECT current_database()                                   AS database,
       pg_database_size(current_database())                 AS bytes,
       pg_size_pretty(pg_database_size(current_database())) AS pretty;

-- 2. Size per table in `public` (data + indexes + TOAST), largest first.
SELECT c.relname                                            AS table_name,
       c.reltuples::bigint                                  AS approx_rows,
       pg_total_relation_size(c.oid)                        AS total_bytes,
       pg_size_pretty(pg_total_relation_size(c.oid))        AS total,
       pg_size_pretty(pg_relation_size(c.oid))              AS heap,
       pg_size_pretty(pg_indexes_size(c.oid))               AS indexes
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
ORDER BY pg_total_relation_size(c.oid) DESC;

-- 3. Average and largest scan_records row (the table that dominates growth).
--    pg_column_size(t.*) is the stored row size without the tuple header or
--    index entries; the growth table adds about 30 percent for those.
SELECT count(*)                                AS row_count,
       round(avg(pg_column_size(t.*)))         AS avg_row_bytes,
       max(pg_column_size(t.*))                AS max_row_bytes
FROM scan_records t;

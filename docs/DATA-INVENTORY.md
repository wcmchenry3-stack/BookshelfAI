# Data inventory

Every table in the `public` schema of the production database: what it holds, whether it is personal data, how long it is kept, which Phase 2 path covers it, and how big it is. Written for #1011 (part of #681).

This page is the per-table index. It does not repeat the retention detail: the rules, purge code, caps and growth tables live in [`storage-and-retention.md`](storage-and-retention.md#table-by-table-retention) (one source of truth; the Retention column here is a short form of it, and the bytes columns reuse its numbers). Column-level schemas are in [`data-model.md`](data-model.md). Backups of all of this: [`backups.md`](backups.md).

The 21 tables below are the 20 ORM tables (`Base.metadata`) plus `alembic_version`, which Alembic owns. `backend/tests/unit/test_data_inventory_doc.py` fails when this table and the schema disagree (see [Keeping this page right](#keeping-this-page-right)). The backup dump (`pg_dump -n public`) holds exactly these tables.

## Phase 2 paths

- **Export** (#694, merged): `backend/app/services/account_export.py`. `section` = a table in `SECTIONS` (the user's rows are in the export file); `catalog` = the shared catalog rows a library entry points at are included, not the whole table; `excluded` = in `EXCLUDED_TABLES` with a reason; `n/a` = holds nothing of the user's.
- **Deletion / anonymisation** (#291, **not built yet**: there is no account-deletion endpoint, gap G1 in the retention doc). The entries say what will happen through the foreign keys once it is: `cascade` = the row goes when its `users` row is deleted; `anonymise` = the row stays with `user_id` set to NULL (`ON DELETE SET NULL`); `delete user` = the `users` row itself; `n/a` = not the user's data.
- **Trash purge** (#683): `user_books` and `user_custom_books` rows in the trash are hard-deleted 30 days after deletion (or beyond the 1,000-per-user cap) by the daily cleanup task. Other tables have their own retention purge, named in the Retention column.

## Tables

Bytes are *estimates* from the retention doc (row size with indexes, from `pg_column_size` where that page says so; production is pre-launch, 12 MB in total). "Per active user" is the steady state the purges allow for a user active every day (3 scans a day, see [Expected growth](storage-and-retention.md#expected-growth)); a table with no purge shows growth instead. Aggregates and configuration are not per user. Rows with "about" and no source in the retention doc (`users`, `books`, `editions`, `device_keys`) are rough sizes to re-measure with `scripts/db/db_size_report.sql` after launch.

| Table | Holds | Personal data | Retention | Export (#694) | Deletion / anonymisation (#291, not built) | Trash purge | Bytes per row | Bytes per active user |
|---|---|---|---|---|---|---|---|---|
| `users` | one row per signed-in person: email, display name, avatar URL, Google ID, default tab, AI consent times, session-revocation counter | Yes | kept until deleted; no delete endpoint yet | `section` | delete user | no | about 300 | about 300 |
| `books` | shared catalog of works from signed provider data (title, author, provider IDs, description, cover URL) | No (public catalog data, no user ID) | none by design | `catalog` | n/a | no | about 1,000 (more with a long description) | shared; with `user_books` and `editions`, about 850 a day per active user (the doc's figure for 1.3 rows) |
| `editions` | shared catalog of editions (ISBNs, publisher, year, format) | No | none by design | `catalog` | n/a | no | about 300 | shared; included in the 850 a day above |
| `user_books` | the user's library rows, live and trashed: status, dates, notes, rating, overrides | Yes (notes and ratings are the user's own words) | live: kept; trashed: 30 days, one trashed copy per book, newest 1,000 per user | `section` | cascade | yes | about 500 typical; at most about 13,000 to 25,000 (long notes) | no purge for live rows: grows with the library, about 500 per book added |
| `user_custom_books` | the user's own unverified entries (title and author typed by hand) | Yes | live: cap 200 per user; trashed with their library row | `section` | cascade | yes | about 150 typical; at most about 1,100 | under 30,000 at the 200 cap |
| `library_events` | append-only audit log of library changes (old and new values; notes and overrides only as "changed") | Yes (user ID, what changed) | 180 days | `section` | anonymise | no (own 180-day purge) | about 400 | about 144,000 (360 rows) |
| `idempotency_keys` | stored responses for replaying an `Idempotency-Key` write (the body is a copy of the user's library entry) | Yes | 24 hour replay window, purged within about 48 hours | `excluded` (transient) | cascade | no (own purge) | about 1,000 | about 4,000 (about 4 rows) |
| `account_exports` | one row per export request: status, email language, link expiry, storage path; the file itself is in Supabase Storage | Yes (the signed link is a credential) | `ready`: until the link expires (7 days); `failed`: 7 days | `section` | cascade (the file is removed by the orphan sweep at 8 days) | no (own purge) | about 150 | under 1,000 (at most about 7 rows) |
| `refresh_tokens` | one row per issued refresh JWT, with the rotation chain for reuse detection | Yes (a session record) | expired after 7 days; revoked-without-rotation rows go at once | `section` | cascade | no (own purge) | about 150 to 190 | about 5,000 (about 30 live rows) |
| `device_keys` | attested device keys per user: iOS App Attest public key and counter, Android marker | Yes (a device identifier tied to the user) | kept while the user exists; stale keys from old installs stay (no purge) | `section` | cascade | no | about 200 | about 200 to 1,000 (1 to 5 devices) |
| `ai_quota` | per-user per-day scan counters and legacy month rows | Yes (user ID, usage counts) | day rows 90 days; month rows 13 months | `section` | cascade | no (own purge) | about 70 | about 19,000 (about 270 rows) |
| `ai_inflight` | one lease row per user with an AI call in flight | Yes (user ID, lease) | deleted on release; a crashed request's row is replaced at the next scan | `excluded` (transient) | cascade | no | about 65 | about 65 |
| `ai_usage` | one row per AI provider call: user ID, tokens, cost, status | Yes (user ID with usage) | 30 days, then rolled up into `ai_usage_daily` and deleted | `section` | anonymise (cost data kept without a user) | no (own purge) | about 80 | about 7,000 (about 90 rows) |
| `ai_usage_daily` | per day, feature, model and status totals | No (no user ID) | 400 days | `n/a` | n/a | no (own purge) | under 100 | none (tens of rows a day in total) |
| `ai_enhanced_zero_results` | uncharged zero-result enhanced scans per user | Yes | 30 days | `section` | cascade | no (own purge) | about 40 | under 1,000 |
| `scan_records` | per-user answer for a scanned photo: image SHA-256, candidates, enrichment JSON (book titles and descriptions; never the photo) | Yes | 30 days at most (setting 1 to 30) and at most 500 per user | `section` | cascade | no (own purge) | about 5,000 typical (1,000 to 10,000; up to about 80,000 for a full shelf) | about 450,000 (90 rows); up to about 2,500,000 at the 500 cap |
| `scan_corrections` | which of title, author and ISBN a user edited on a pre-filled save | Yes (reaches the user through `scan_records`) | 30 days; also goes with its scan record | `section` | cascade (through `scan_records`) | no (own purge) | about 60 | about 200 |
| `scan_corrections_daily` | per day, feature and model correction counts | No | 400 days | `n/a` | n/a | no (own purge) | under 100 | none (tens of rows a day in total) |
| `ai_spend_daily` | one row per UTC day: reserved and settled AI spend for the cost circuit breaker | No | 90 days | `n/a` | n/a | no (own purge) | under 60 | none (one row a day in total) |
| `ai_settings` | operator dials (fixed keys and values) | No | fixed set of keys; does not grow | `n/a` | n/a | no | under 300 | none (about 20 rows in total) |
| `alembic_version` | the Alembic migration revision the database is at | No | one row | `n/a` | n/a | no | under 100 | none (1 row) |

Not in Postgres: the account export files (private Supabase Storage bucket `account-exports`, deleted once the 7-day link expires, see [Account export files](storage-and-retention.md#account-export-files-supabase-storage)) and the phone's scan queue (see [On the phone](storage-and-retention.md#on-the-phone)). Logs (Render 14 days, Sentry 30 days) are under [Logs](storage-and-retention.md#logs). The photo itself is never stored on the server.

## Reading the table

- **Personal data** means the row can be tied to a person or holds their words. The shared catalog and aggregate tables carry no user ID.
- **Total per active user**: the steady-state rows above add up to about 0.65 MB for a user who scans 3 books a day, of which `scan_records` is about 70 percent. Library growth is on top and has no purge for live rows.
- **Account deletion (#291)** needs to cover exactly the user-owned tables (`users`, every table with a foreign key to it, and `scan_corrections` through `scan_records`). The test checks that every such table is marked as personal data here, so the deletion story starts from a complete list.

## Keeping this page right

`backend/tests/unit/test_data_inventory_doc.py` fails when:

- a table in `Base.metadata` (or `alembic_version`) has no row here, or a row names a table that does not exist;
- a user-owned table (`backend/tests/_user_tables.py`) is not marked as personal data;
- the Export column disagrees with `SECTIONS` and `EXCLUDED_TABLES` in `account_export.py`.

A story that adds a table adds its row here and in the [retention table](storage-and-retention.md#table-by-table-retention) (that page has its own drift test). When account deletion (#291) lands, update the deletion column from "not built" to the real behaviour in the same PR.

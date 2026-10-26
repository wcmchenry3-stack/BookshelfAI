# Data model

PostgreSQL schema, entity reference, and the dedup strategy that makes the collection work at the work-level instead of per-edition. For the architectural rationale behind dedup see [`architecture.md`](architecture.md#adr-work-level-book-deduplication-open-library-work_id); for migration mechanics see the [Migration workflow](#migration-workflow) section below.

## ER diagram

```mermaid
erDiagram
  USERS ||--o{ USER_BOOKS : owns
  USERS ||--o{ REFRESH_TOKENS : issued
  USERS ||--o{ SCAN_USAGE_DAILY : "scans per day (legacy)"
  USERS ||--o{ AI_QUOTA : "AI calls per window"
  USERS ||--o| AI_INFLIGHT : "call in flight"
  USERS |o--o{ AI_USAGE : "provider calls (set null on delete)"
  USERS ||--o{ AI_ENHANCED_ZERO_RESULTS : "recorded empty enhanced scans"
  USERS ||--o{ SCAN_RECORDS : "scan records"
  SCAN_RECORDS ||--o{ SCAN_CORRECTIONS : "correction flags (#631, cascade)"
  USERS ||--o{ USER_CUSTOM_BOOKS : "own unverified entries (#616)"
  USER_CUSTOM_BOOKS ||--o| USER_BOOKS : "held by (cascade)"
  BOOKS ||--o{ EDITIONS : "has editions"
  BOOKS ||--o{ USER_BOOKS : "tracked by"
  EDITIONS ||--o{ USER_BOOKS : "optional edition"

  USERS {
    uuid id PK
    string email UK
    string google_id UK
    string display_name
    string avatar_url
    int enhanced_scan_credits "legacy, unused since #533; dropped in #540"
    timestamptz created_at
    timestamptz updated_at
  }
  BOOKS {
    uuid id PK
    string open_library_work_id UK "indexed"
    string google_books_id
    string title
    string author
    string description
    string cover_url
    string_array subjects
    string language "default 'en'"
    timestamptz created_at
    timestamptz updated_at
  }
  EDITIONS {
    uuid id PK
    uuid book_id FK "indexed, restrict delete"
    string isbn_13 UK
    string isbn_10
    string publisher
    int publish_year
    string format "check constraint"
    int page_count
    string open_library_edition_id
    timestamptz created_at
  }
  USER_BOOKS {
    uuid id PK
    uuid user_id FK "indexed, cascade delete"
    uuid book_id FK "restrict delete; null for an unverified entry"
    uuid custom_book_id FK,UK "nullable, cascade delete (#616)"
    uuid edition_id FK "optional"
    string status "indexed, check constraint"
    timestamptz wishlisted_at
    timestamptz purchased_at
    timestamptz started_at
    timestamptz finished_at
    string notes
    int rating "1..5"
    timestamptz created_at
    timestamptz updated_at
  }
  REFRESH_TOKENS {
    uuid id PK
    string jti UK "indexed"
    uuid user_id FK "indexed, cascade delete"
    timestamptz issued_at
    timestamptz expires_at
    timestamptz revoked_at
  }
  SCAN_USAGE_DAILY {
    uuid user_id PK,FK "cascade delete"
    date usage_date PK "UTC, indexed"
    int scans
  }
  AI_QUOTA {
    uuid user_id PK,FK "cascade delete"
    varchar feature PK "quota key: scan"
    varchar window_kind PK "day (month: legacy, #582)"
    date window_start PK "UTC day"
    int units_used ">= 0"
  }
  AI_INFLIGHT {
    uuid user_id PK,FK "cascade delete"
    uuid lease_id
    timestamptz lease_expires_at
  }
  AI_USAGE {
    bigint id PK
    uuid call_id UK "server-generated per call"
    uuid user_id FK "nullable, set null on delete"
    varchar feature
    varchar model
    varchar price_version
    int input_tokens ">= 0"
    int output_tokens ">= 0"
    int cache_read_tokens ">= 0"
    int cache_write_tokens ">= 0"
    bigint cost_micros ">= 0"
    varchar status "check constraint"
    varchar request_id "nullable"
    smallint attempt "nullable, 1..6, #603"
    timestamptz created_at "indexed"
  }
  AI_USAGE_DAILY {
    date day PK "UTC"
    varchar feature PK
    varchar model PK
    varchar status PK
    bigint calls
    bigint input_tokens
    bigint output_tokens
    bigint cost_micros
  }
  AI_ENHANCED_ZERO_RESULTS {
    uuid id PK
    uuid user_id FK "cascade delete"
    timestamptz created_at "indexed with user_id"
    uuid ledger_id "nullable, ai_usage.call_id (no FK)"
    boolean warned "row that sent the threshold warning"
  }
  SCAN_RECORDS {
    uuid id PK
    uuid user_id FK "cascade delete, unique with fingerprint"
    varchar fingerprint "sha256 hex of the normalised JPEG"
    varchar feature
    varchar tier
    varchar model
    jsonb candidates "validated candidates only"
    jsonb enrichment "nullable, enriched books"
    boolean ai_done
    boolean enrich_done
    timestamptz created_at "indexed"
    timestamptz updated_at
  }
  SCAN_CORRECTIONS {
    bigint id PK
    uuid scan_record_id FK "cascade delete, unique with candidate_index"
    smallint candidate_index "check >= 0"
    varchar feature
    varchar tier
    varchar model
    boolean title_edited
    boolean author_edited
    boolean isbn_edited
    timestamptz created_at "indexed"
  }
  SCAN_CORRECTIONS_DAILY {
    date day PK "UTC"
    varchar feature PK
    varchar model PK
    bigint prefilled_saves
    bigint title_edited
    bigint author_edited
    bigint isbn_edited
    bigint any_edited
  }
  AI_SPEND_DAILY {
    date day PK "UTC"
    bigint reserved_micros "check >= 0"
    bigint settled_micros "check >= 0"
    smallint alerted_mask "bit per alert threshold"
  }
  AI_SETTINGS {
    text key PK
    jsonb value
    timestamptz updated_at
    text updated_by "nullable"
  }
```

All primary keys are `UUID` (Postgres native `uuid` type, generated server-side via `uuid.uuid4`), except `ai_usage.id` and `scan_corrections.id` (`BIGSERIAL`) and the composite keys of the counter tables. All timestamps are `TIMESTAMPTZ` (UTC).

**Deletion semantics.** Shared catalog rows (`books`, `editions`) must never take user data with them:

| FK | `ON DELETE` | Effect |
|---|---|---|
| `user_books.user_id` → `users.id` | `CASCADE` | Deleting an account removes that user's library (intentional). |
| `refresh_tokens.user_id` → `users.id` | `CASCADE` | Deleting an account removes its refresh tokens. |
| `scan_usage_daily.user_id`, `ai_quota.user_id`, `ai_inflight.user_id`, `ai_enhanced_zero_results.user_id` → `users.id` | `CASCADE` | Deleting an account removes its usage counters, any lease and its zero-result enhanced-scan rows. |
| `scan_records.user_id` → `users.id` | `CASCADE` | Deleting an account removes its scan records. |
| `scan_corrections.scan_record_id` → `scan_records.id` | `CASCADE` | A scan record's correction flags go with it (retention purge, per-user cap, account deletion); the daily totals in `scan_corrections_daily` (no user) stay. |
| `ai_usage.user_id` → `users.id` | `SET NULL` | Deleting an account keeps the cost history but removes the personal link (`user_id` becomes NULL). |
| `user_books.book_id` → `books.id` | `RESTRICT` | A book referenced by any user's library cannot be deleted (`IntegrityError`). |
| `editions.book_id` → `books.id` | `RESTRICT` | A book that still has editions cannot be deleted. |
| `user_books.edition_id` → `editions.id` | none (`NO ACTION`) | An edition referenced by a `UserBook` cannot be deleted. |

An unreferenced book can be deleted. The ORM relationships `Book.editions` and `Book.user_books` have no delete cascade and use `passive_deletes="all"`, so the ORM never deletes or nulls out children (even when they are loaded in the session) and the `ON DELETE RESTRICT` FKs reject the delete (migration `0004`).

## Entity reference

### `users` (`backend/app/models/user.py`)

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | generated |
| `email` | string, **unique**, not null | authenticated email |
| `google_id` | string, **unique**, not null | Google OAuth `sub` claim |
| `display_name` | string, nullable | from Google profile |
| `avatar_url` | string, nullable | from Google profile |
| `enhanced_scan_credits` | int, not null, server default 5 | **legacy:** the old lifetime enhanced-scan balance (migration 0003). Since #533 nothing in the app reads or writes it (enhanced scans use the Premium enhanced allowance in `ai_quota`, quota key `scan.enhanced`); new rows get the server default. Dropped in #540 |
| `created_at` / `updated_at` | timestamptz, server defaults | `updated_at` auto-bumps on row change |

**Lockdown:** the backend enforces `ALLOWED_EMAILS` at the `/auth/google` endpoint — any Google account whose email isn't in that comma-separated env var is rejected *before* a User row is created. See [`architecture.md`](architecture.md#auth-architecture).

### `books` (`backend/app/models/book.py`)

Represents the **conceptual work** — the thing a user would say "I own Dune" about. Editions are separate.

**Who writes `books` and `editions` (#602).** Only `WishlistService` (`POST /wishlist`) creates them, and only from a book whose server-signed `catalog_token` verifies (data the server produced from Open Library / Google Books for that user). Without one it may only reference existing rows; a book with no provider ID (no `open_library_work_id`, no `google_books_id`) is never signed or created. Signed fields are provider data only (title and author from the Open Library / Google Books record, ISBNs only when a provider record used lists them; a Google Books volume that disagrees with the Open Library doc is not used). `books.open_library_work_id`, `books.google_books_id` (when set, #613) and `editions.isbn_13` are unique, and a racing duplicate insert links the existing row (the loser of the race re-reads by the work ID, then the volume ID). Lookups by `google_books_id` still take the oldest row (`created_at`, `id`) and the library check only tests existence, a harmless guard from before the index (#611). No request ever updates an existing `books` or `editions` row. See [security.md](security.md#shared-catalog-trust-model).

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | generated |
| `open_library_work_id` | string, **unique**, indexed, nullable | primary dedup key ([ADR](architecture.md#adr-work-level-book-deduplication-open-library-work_id)) |
| `google_books_id` | string, nullable, **unique when not null** (partial index `uq_books_google_books_id`, #613) | fallback dedup key when Open Library has no work_id. Migration 0015 merged the duplicates that existed before the index |
| `title` | string, not null | |
| `author` | string, not null | first author; additional authors rolled into the string |
| `description` | string, nullable | from Open Library or Google Books |
| `cover_url` | string, nullable | Google Books cover URL (typically) |
| `subjects` | text[], nullable | Open Library subject tags |
| `language` | string, default `'en'`, not null | ISO 639-1 code |
| `created_at` / `updated_at` | timestamptz, server defaults | |

**Relationships:**
- `editions` → `Edition[]` (no cascade — a Book with Editions cannot be deleted)
- `user_books` → `UserBook[]` (no cascade — a Book in any user's library cannot be deleted)

### `editions` (`backend/app/models/edition.py`)

Specific publication of a Book. Captures ISBN, publisher, year, format.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | generated |
| `book_id` | UUID FK → `books.id`, indexed, restrict delete, not null | |
| `isbn_13` | string, **unique**, nullable | canonical identifier |
| `isbn_10` | string, nullable | legacy form |
| `publisher` | string, nullable | |
| `publish_year` | int, nullable | |
| `format` | string, nullable | check constraint: `hardcover`, `paperback`, `ebook`, `audiobook`, `unknown` |
| `page_count` | int, nullable | |
| `open_library_edition_id` | string, nullable | Open Library's per-edition ID |
| `created_at` | timestamptz, server default | no `updated_at` — editions are immutable once captured |

### `user_books` (`backend/app/models/user_book.py`)

The user's relationship to a Book — this is the "bookshelf" row.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | generated |
| `user_id` | UUID FK → `users.id`, indexed, cascade delete, not null | |
| `book_id` | UUID FK → `books.id`, restrict delete, nullable | the catalog book; `NULL` only for an unverified entry (#616) |
| `custom_book_id` | UUID FK → `user_custom_books.id`, cascade delete, nullable, unique (`uq_user_books_custom_book`) | the user's own unverified entry (#616); `ck_user_books_one_book` requires exactly one of `book_id` / `custom_book_id` |
| `edition_id` | UUID FK → `editions.id`, nullable (no cascade) | nullable because wishlisted items often don't have an edition picked yet |
| `status` | string, indexed, not null | check constraint: `wishlisted`, `purchased`, `reading`, `read` |
| `wishlisted_at` | timestamptz, nullable | set when status first enters `wishlisted` |
| `purchased_at` | timestamptz, nullable | set on wishlisted→purchased transition |
| `started_at` | timestamptz, nullable | set on purchased→reading transition (or on re-read) |
| `finished_at` | timestamptz, nullable | set on reading→read transition |
| `notes` | string, nullable | user notes |
| `rating` | int, nullable | check constraint: 1..5 |
| `created_at` / `updated_at` | timestamptz, server defaults | |

**Uniqueness:** `(user_id, book_id)` is a unique constraint (`uq_user_books_user_book`). A user can have at most one tracking row per work — this is what dedup is enforcing. Different editions of the same work still share one `UserBook`; changing edition updates `edition_id` in place. (Unverified rows have `book_id` NULL, which the constraint does not compare; their duplicates are refused by `UnverifiedBookService`.)

### `user_custom_books` (`backend/app/models/user_custom_book.py`)

Unverified entries (#616, migration 0016): books the user added themselves because neither Open Library nor Google Books found them. **Private to the creator and outside the shared catalog**: nothing that reads `books` / `editions` (catalog lookups, the library check, `WishlistService`) can see them, and only the creator's own `user_books` row references one.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | generated; shown as `book.id` in `UserBookRead` |
| `user_id` | UUID FK → `users.id`, indexed, cascade delete, not null | the creator |
| `title` | string, not null | check: 1–500 characters |
| `author` | string, not null | check: 1–500 characters |
| `isbn_13` | string, nullable | the ISBN the user typed, normalised to ISBN-13 (check: 13 characters). **Not unique and never matched against anyone else's data**: it never claims `editions.isbn_13` |
| `created_at` / `updated_at` | timestamptz, server defaults | |

No provider IDs, cover, description or edition. At most `UNVERIFIED_BOOKS_PER_USER_MAX` (default 200) rows per user; each row is at most ~1.1 KB (typically ~150 B), so ~220 KB per user worst case, and writes are rate limited (`rate_limit_writes`). Rows go with the account (cascade) and with their library row (`DELETE /user-books/{id}` deletes both). Account deletion and data export (not built yet) must include this table. Downgrading 0016 deletes every unverified entry.

### `refresh_tokens` (`backend/app/models/refresh_token.py`)

Backs the refresh-rotation + revocation mechanism ([architecture.md](architecture.md#auth-architecture)).

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | generated |
| `jti` | string, **unique**, indexed, not null | the `jti` claim inside the refresh JWT |
| `user_id` | UUID FK → `users.id`, indexed, cascade delete, not null | |
| `issued_at` | timestamptz, server default | |
| `expires_at` | timestamptz, not null | issued + `REFRESH_TOKEN_EXPIRY_DAYS` (default 7) |
| `revoked_at` | timestamptz, nullable | set on `/auth/refresh` (old jti) or `/auth/logout` |

**Lifecycle:** `/auth/refresh` validates the presented refresh JWT → checks `jti` exists and `revoked_at IS NULL` → issues a new access+refresh pair, inserts a new row, and sets `revoked_at = now()` on the old row. The effect is that **refresh tokens are single-use** — a stolen copy is invalidated by the legitimate client's next refresh.

### `scan_usage_daily` (`backend/app/models/scan_usage.py`)

**Legacy since #528:** `/scan` no longer reads or writes it (the caps moved to `ai_quota` below). The table, `app/services/scan_quota.py` and its purge stay until #540 removes them; existing rows age out through the purge.

Counts scans that reached the vision provider, per user per UTC day. Backed the per-user and global daily scan caps of #509.

| Column | Type | Notes |
|---|---|---|
| `user_id` | UUID FK → `users.id`, cascade delete, PK part | |
| `usage_date` | date, PK part, indexed (`ix_scan_usage_daily_usage_date`) | UTC day |
| `scans` | int, not null, server default 0 | incremented by the reservation upsert, decremented (never below 0) on a refund |

**Keys:** the composite primary key `(user_id, usage_date)` is the unique key the reservation upsert targets. The `usage_date` index serves the global daily sum and the retention purge.

**Volume and retention:** one row per active user per day (~40 bytes of data, roughly 100 bytes on disk with the indexes). The daily cleanup task (`app/services/token_cleanup.py`, calling `scan_quota.purge_old_scan_usage`) deletes rows with `usage_date` older than 90 days, in batches of 5,000 and at most 20 batches per run. Deleting a user removes their rows by cascade.

### `ai_quota` (`backend/app/models/ai_quota.py`)

Per-user count of AI calls reserved, per quota key and window (#528). Backs the daily found and no-match scan allowances (#577), the global daily ceiling and, under its own key, the per-user daily `/scan` request limit (#586). There is no monthly allowance since #582: month rows are legacy, no longer read or written ([architecture.md](architecture.md#book-identification--dedup-pipeline)).

| Column | Type | Notes |
|---|---|---|
| `user_id` | UUID FK → `users.id`, cascade delete, PK part | |
| `feature` | varchar(64), PK part | the quota key: `scan` is the **found** counter, the unit every scan, standard or enhanced, reserves before the provider (one shared allowance, OD-1; `quota.SCAN_QUOTA_FEATURE`); `scan.no_match` is the **no-match** counter (#577; `quota.SCAN_NO_MATCH_QUOTA_FEATURE`): standard scans whose provider answered but found no book, moved off `scan` in the settle while under `quota.<tier>.no_match_daily`. Both are day rows; the global daily sum counts both. `scan.requests` is the **/scan request** counter (#586; `quota.SCAN_REQUESTS_QUOTA_FEATURE`): every `/scan` request past authentication and the kill switches, new photo or repeat, allowed or refused later, taken with the same conditional upsert while under `scan.requests_per_user_daily` and never given back. It counts requests, not AI calls, so the global daily sum leaves it out (`quota.NON_AI_QUOTA_FEATURES`). Legacy: `scan.enhanced` month rows, from the Premium enhanced monthly allowance #582 removed |
| `window_kind` | varchar(8), PK part, CHECK `IN ('day', 'month')` | `day`; `month` rows are legacy (written before #582, never since) |
| `window_start` | date, PK part | the UTC day (legacy month rows: the 1st of the UTC calendar month) |
| `units_used` | int, not null, server default 0, CHECK `>= 0` | incremented by the reservation upsert, decremented (never below 0) on a refund or (found, #577) when a no-match moves to `scan.no_match`; `scan.requests` rows are only ever incremented (#586) |

**Keys and indexes:** the composite primary key `(user_id, feature, window_kind, window_start)` is the unique key the reservation upsert targets (`INSERT … ON CONFLICT … DO UPDATE SET units_used = units_used + 1 WHERE units_used < limit`). `ix_ai_quota_window (window_kind, window_start)` serves the global daily sum (which filters out `scan.requests`) and the retention purge.

**Writes:** only `app/ai/quota.py` (reserve, refund, the no-match move, the request count, purge), from the request's server-side user and the server's quota key. No client-supplied value is stored.

**Volume and retention:** about 70 bytes per row. Per active user, up to three day rows for each day they scan (`scan`, `scan.no_match` on a day with a no-match, #577, and `scan.requests` on any day they call `/scan`, #586): at most about 210 bytes per active user per day. `scan.requests` rows are purged with the other day rows. No month rows are written since #582. The daily cleanup task (`app/services/token_cleanup.py`, calling `quota.purge_old_quota`) deletes day rows older than 90 days and month rows whose month began more than 13 months before the current month (so the legacy month rows are gone 13 months after the last one was written; until then they are kept so a rollback to the previous release finds its counters), in batches of 5,000 and at most 20 batches per window kind per run. This table is not used for reporting: monthly spend and call counts come from `ai_usage` / `ai_usage_daily`.

### `ai_inflight` (`backend/app/models/ai_quota.py`)

The per-user lease that allows one paid AI call in flight at a time (#528).

| Column | Type | Notes |
|---|---|---|
| `user_id` | UUID FK → `users.id`, cascade delete, PK | at most one row per user |
| `lease_id` | UUID, not null | random per acquisition; the release deletes the row only while it still holds this id, so a slow request whose lease expired and was taken over cannot release the new holder's lease |
| `lease_expires_at` | timestamptz, not null | `clock_timestamp()` + the feature's `timeout_s` + 30 s; an expired lease can be taken over |
| `daily_taken` | boolean, not null, server default false | the holder took its daily `ai_quota` unit (#590, migration 0012); set in the same commit as the increment, cleared in the same commit as the give-back |

**Writes:** acquired with `INSERT … ON CONFLICT (user_id) DO UPDATE … WHERE ai_inflight.lease_expires_at < clock_timestamp()` (a takeover also resets the flag), deleted when the call settles or is refunded (always, in a `finally`). Each unit flag is set with `UPDATE … WHERE user_id = ? AND lease_id = ?` in the transaction that takes the unit, and cleared with `UPDATE … WHERE user_id = ? AND lease_id = ? AND <flag> RETURNING` in the transaction that gives it back, which decrements only when a row came back, so a retried give-back never gives a unit back twice ([architecture.md](architecture.md)).

**Volume and retention:** at most one row per user with a call in flight (~65 bytes; the flags add 3 bytes and no rows). Rows are deleted on release; a row left by a crashed request stays until that user's next scan takes it over (it is harmless once expired), or the user is deleted.

### `ai_usage` (`backend/app/models/ai_usage.py`)

The AI usage ledger: one row per paid provider call (#530). Counts and cost only; it never stores the prompt, the image or the model's output (`tests/security/test_ai_usage_ledger_content.py` asserts the exact column set).

| Column | Type | Notes |
|---|---|---|
| `id` | BIGSERIAL, PK | |
| `call_id` | UUID, not null, unique (`uq_ai_usage_call_id`) | generated by the server (`uuid4`) once per provider call; the insert is `ON CONFLICT (call_id) DO NOTHING`, so a settle retried after a commit whose acknowledgement was lost adds no second row |
| `user_id` | UUID FK → `users.id`, nullable, `ON DELETE SET NULL` | NULL after the account is deleted |
| `feature` | varchar(64), not null | gateway feature, e.g. `scan.standard`, `scan.enhanced` |
| `model` | varchar(64), not null | the model the call used (registry default or `ai_settings` override) |
| `price_version` | varchar(16), not null | the price-table version the cost was computed at (`app/ai/prices.py`) |
| `input_tokens` | int, not null, ≥ 0 | prompt tokens billed at the full input price (excludes cached tokens) |
| `output_tokens` | int, not null, ≥ 0 | completion tokens |
| `cache_read_tokens` | int, not null, ≥ 0 | prompt tokens served from the provider's cache |
| `cache_write_tokens` | int, not null, ≥ 0 | prompt tokens written to the provider's cache (0 for OpenAI) |
| `cost_micros` | bigint, not null, ≥ 0 | cost in micro-dollars (1,000,000 = $1), each count × its list price, rounded up |
| `status` | varchar(16), not null, check | `ok`, `refused` (a `refusal` or content filter), `truncated` (the provider answered), `malformed` (#572: the provider answered HTTP 200 and billed the call, but the reply was missing, not JSON, the wrong shape, a non-empty list in which every item was structurally broken, ended on an unexpected stop reason, or was cut off by the token limit before a single complete book; the real tokens and cost are recorded and settled, the user's units are refunded and the client gets `503 scan_unavailable`; added to the CHECK by migration 0013), `timeout`, `refunded` (unavailable or never reached; the quota was refunded), `error` (anything else, including a cancelled request; the quota was kept), `cache_hit` (a repeat served from a scan record, #585: no provider call, 0 tokens, cost always 0) |
| `request_id` | varchar(64), nullable | the server-generated `X-Request-ID`; never client-supplied |
| `attempt` | smallint, nullable | which try of the `/scan` request wrote the row (#603, migration 0014): the request's `attempt` form field, 1 the first try, 2–6 the app's scheduled retries (#574); a missing field is stored as 1. The only client-supplied value in the table: FastAPI validates it to 1–6 before the handler runs (anything else is a `422` with nothing written). Set on every row a `/scan` request writes (`ok`, `refused`, `truncated`, `malformed`, `refunded`, `timeout`, `error` and a repeat's `cache_hit`). NULL on rows written before 0014 (unknown). Purely informational: never read for charging, limits or ordering, and not carried into `ai_usage_daily` (the 30 days of raw rows are enough to count retries) |
| `created_at` | timestamptz, not null, server default `now()` | indexed (`ix_ai_usage_created_at`) |

**Writes:** `/scan` builds the row (`app/ai/ledger.py`, `usage_insert`) and hands it to `quota.release`, which executes it just before the lease delete, so the row and the settle commit in one transaction. A cancelled request still writes an `error` row (0 tokens) through the shielded settle, since its reservation is kept. A kill switch hit inside the gateway (`FeatureDisabledError`, no provider reached) writes none. If the row is rejected (CHECK or FK), it is dropped (logged, no content) and the lease is released on its own; if the settle times out, nothing more is tried and the lease expiry is the backstop. A ledger failure never fails the scan. `ledger.record` commits on its own and is only for paths with no settle: a repeat served from a scan record (#585) writes its `cache_hit` row this way, with its own `call_id`. Costs come from the list-price table in `app/ai/prices.py`, keyed by `(model, price_version)`; an unknown pair raises instead of pricing at zero, and every allow-listed model must have a price at the current version (checked at import and in CI).

**Keys and indexes:** `ix_ai_usage_created_at (created_at)` for the rollup; `ix_ai_usage_user_created (user_id, created_at)` for per-user lookups and the `SET NULL` on account deletion.

**Volume and retention:** about 80 bytes per row (`attempt` adds 2), one row per provider call. Raw rows are kept 30 days: the daily cleanup task (`app/services/token_cleanup.py`, calling `ledger.rollup_and_purge`) moves older rows into `ai_usage_daily` and deletes them, in batches of 5,000 and at most 20 batches per run. Each batch deletes its rows (`DELETE … RETURNING`) and adds their totals to the daily rows in one transaction, so a row is never counted twice or lost, and a second run changes nothing.

### `ai_usage_daily` (`backend/app/models/ai_usage.py`)

Daily totals of `ai_usage` per UTC day, feature, model and status (#530). No user column.

| Column | Type | Notes |
|---|---|---|
| `day` | date, PK | UTC day of `ai_usage.created_at` |
| `feature` | varchar(64), PK | |
| `model` | varchar(64), PK | |
| `status` | varchar(16), PK, check | same values as `ai_usage.status` |
| `calls` | bigint, not null, ≥ 0 | number of raw rows |
| `input_tokens` | bigint, not null, ≥ 0 | sum of `ai_usage.input_tokens` (cache tokens are not carried over) |
| `output_tokens` | bigint, not null, ≥ 0 | sum of `ai_usage.output_tokens` |
| `cost_micros` | bigint, not null, ≥ 0 | sum of `ai_usage.cost_micros` (includes cache costs) |

**Writes:** only the rollup, as an upsert that adds to an existing row (`INSERT … ON CONFLICT (day, feature, model, status) DO UPDATE SET calls = calls + excluded.calls, …`).

**Volume and retention:** tens of rows per day (features × models × statuses), each under 100 bytes. Rows are kept 400 days; the same cleanup run deletes older ones in bounded batches.

**Cost per scan and correction rate (#631).** Cost per scan comes from `ai_usage` (the last 30 days) and `ai_usage_daily` (older days), per day, feature and model. Next to it, `scan_corrections_daily` (below) gives how often users corrected the AI's reading when they saved an "Add it yourself" entry pre-filled from a scan, on the same key, for every day including today (it is counted when the save happens, not after 30 days). For example, per model over the last 30 days:

```sql
SELECT feature, model,
       SUM(any_edited)::float    / NULLIF(SUM(prefilled_saves), 0) AS correction_rate,
       SUM(title_edited)::float  / NULLIF(SUM(prefilled_saves), 0) AS title_rate,
       SUM(author_edited)::float / NULLIF(SUM(prefilled_saves), 0) AS author_rate,
       SUM(isbn_edited)::float   / NULLIF(SUM(prefilled_saves), 0) AS isbn_rate
FROM scan_corrections_daily
WHERE day >= CURRENT_DATE - 30
GROUP BY feature, model;
```

Join it with the cost per scan on `(day, feature, model)` (sum `cost_micros` and `calls` over the statuses of `ai_usage_daily`, plus the raw `ai_usage` rows of the last 30 days) to compare models on both. The rate only covers results no catalog matched (the only ones "Add it yourself" is offered for), so it is a signal of reading quality on hard spines, not of every scan.

### `ai_enhanced_zero_results` (`backend/app/models/ai_enhanced_zero_result.py`)

One row per recorded enhanced scan that returned no books (#533). Before #582 such a scan was not charged an enhanced monthly unit and the rows stopped that from being farmed; with no enhanced monthly unit any more (the daily unit is always charged) they remain the abuse signal behind the warning and the throttle: the per-user count inside `scan.enhanced.zero_result_window_hours` is compared with `scan.enhanced.zero_result_threshold` (see [architecture.md](architecture.md#book-identification--dedup-pipeline)).

| Column | Type | Notes |
|---|---|---|
| `id` | UUID, PK | generated (`uuid4`) |
| `user_id` | UUID FK → `users.id`, not null, `ON DELETE CASCADE` | |
| `created_at` | timestamptz, not null, server default `now()` | set to `clock_timestamp()` by the insert; the window is measured on the same database clock |
| `ledger_id` | UUID, nullable, no FK | the scan's `ai_usage.call_id`; NULL when the settle had no ledger row. No FK because `ai_usage` rows are rolled up and deleted after 30 days |
| `warned` | boolean, not null, server default `false` | `true` on the row whose insert sent the threshold warning. A new warning is sent only while no `warned` row of the user is inside the window: at most one warning per user per window |

**Index:** `ix_ai_enhanced_zero_results_user_created (user_id, created_at)` serves the window count (`WHERE user_id = ? AND created_at > clock_timestamp() - window`) and the throttle's Retry-After lookup.

**Writes:** only `app/ai/quota.py` (`record_zero_result`), inside the scan's settle transaction (`quota.release`): the `ai_usage` insert, the cost settle, this row and the lease delete commit together (before #582 also the refund of the `scan.enhanced` month unit; there is no such unit any more). It is applied only when that ledger insert returned a row (without a ledger row, only on the first attempt), so a settle retried after a lost commit acknowledgement writes no second row, and only while the request still holds its lease (its `ai_inflight` row is locked `FOR UPDATE` until the commit), so a request whose lease was taken over writes nothing. The insert also sets `warned` when it claims the threshold warning, so the claim commits with the row. The row is written only when the user was below the threshold; an empty scan at or above it, a scan with books and any provider error, timeout or malformed reply write nothing. The scan's daily unit stays charged either way (#582). A repeat served from a scan record (#585) makes no provider call and writes none. No client-supplied value is stored: no image, hash or content.

**Volume and retention:** about 40 bytes per row (more with the index), bounded by the threshold per user per window (at most 20 per window at the maximum setting). Rows older than 30 days are deleted by the daily cleanup task (`app/services/token_cleanup.py`, calling `quota.purge_old_zero_results`) in batches of 5,000 and at most 20 batches per run. Deleting a user removes their rows by cascade. **Phase 2 account deletion and data export must include this table.**

### `scan_records` (`backend/app/models/scan_record.py`)

Per-user scan records by photo fingerprint (#585, decision #573): a user who uploads the exact same photo again gets the saved result, with no provider call, no enrichment call and no allowance used; if only the enrichment step is missing, only that step is redone (see [architecture.md](architecture.md#book-identification--dedup-pipeline)). Only `app/ai/scan_records.py` reads and writes it. Keyed on the user and the fingerprint only, not the prompt or model, so an old answer stays valid when either changes.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `user_id` | UUID FK → `users.id`, not null, `ON DELETE CASCADE` | from the session, never the request |
| `fingerprint` | varchar(64), not null | SHA-256 hex of the normalised JPEG bytes (`app/ai/images.py`), the bytes the provider would get. Never the image itself |
| `feature` | varchar(64), not null | the gateway feature that produced `candidates` (`scan.standard`, `scan.enhanced`); an enhanced scan is only answered by an enhanced record |
| `tier` | varchar(16), not null | the user's tier at the time (`free`, `premium`) |
| `model` | varchar(64), not null | the model that produced `candidates` |
| `candidates` | JSONB, not null | the AI step: the validated candidate list (`title`, `author`, `confidence`, `isbn_13`, `isbn_10` per book). Never the model's raw output. Validated again on read; a row that does not validate is treated as no record |
| `enrichment` | JSONB, nullable | the enrichment step: the enriched books (the `EnrichedBook` fields from Open Library / Google Books, without `book_id`, `catalog_token` and the per-user library flag, which is checked on every scan; each book also holds `record_v: 2`, #602). NULL until the step finished; validated on read, and treated as missing if it does not validate or a book lacks the current `record_v` (saved before catalog fields were provider-only), so a repeat redoes the lookups |
| `ai_done` | boolean, not null, default false | true once the AI step is saved (rows are only written with it true) |
| `enrich_done` | boolean, not null, default false | true once every Open Library / Google Books lookup for the answer finished |
| `created_at` | timestamptz, not null | when the AI step was saved; retention runs from it |
| `updated_at` | timestamptz, not null | the last step saved |

Constraints: `uq_scan_records_user_fingerprint (user_id, fingerprint)`; `ck_scan_records_fingerprint` (64 characters); `ck_scan_records_steps` (`enrich_done` needs `ai_done` and a non-null `enrichment`).

**Indexes:** the unique `(user_id, fingerprint)` for the lookup; `ix_scan_records_user_created (user_id, created_at)` for the per-user eviction and the cascade; `ix_scan_records_created_at (created_at)` for the purge.

**Writes:** after a successful provider call (outcome `ok`, with books or a genuine no-match, #572), `/scan` saves the record step by step after the settle, outside the lease. The AI step is an upsert on `(user_id, fingerprint)` that sets the candidates, feature, tier and model, clears the enrichment (stored as SQL NULL, not JSON `null`) and resets `created_at`, then deletes the user's rows beyond `scan_records.max_per_user` (default 500) oldest first (at most 1,000 per save), in one transaction. An existing `scan.enhanced` record that is still within `scan_records.retention_days` is never replaced by a `scan.standard` one (a standard and an enhanced scan of the same new photo running at once); an expired one is replaced, since lookups no longer serve it; the skipped save leaves the row's `created_at` unchanged, so that standard scan's enrichment step matches nothing. The enrichment step is an update matched on the AI step's `created_at` (so it never lands on a newer answer), saved only when every lookup finished; a repeat whose enrichment step is missing saves it the same way. Both are idempotent: re-running one (a commit whose acknowledgement was lost, then a retry) leaves the same single row. A genuine no-match (an empty `ok` list) is saved as a finished record with empty `candidates` and `enrichment`, so its re-upload is a free repeat (#572, owner decision on #573). Refusals, truncated or malformed replies, errors, timeouts and unavailability are never saved. Reads serve only rows of the requesting user with `ai_done` and `created_at` within `scan_records.retention_days`, and at most `SCAN_MAX_BOOKS` candidates or books (checked on write and read). A read or write failure is logged without content and never fails the scan. No client-supplied value is stored. The record's `id` is returned as `scan_record_id` on `/scan` (#631) so an "Add it yourself" save can name the candidate it came from; it is only ever looked up together with the session's user.

**Volume and retention:** usually 1–10 KB per row (the enrichment carries book descriptions of up to 5,000 characters; a full shelf of 15 books can approach 80 KB). Bounded per user by `scan_records.max_per_user` and by the retention: rows older than `scan_records.retention_days` (default 30, at most 30) are deleted by the daily cleanup task (`app/services/token_cleanup.py`, calling `scan_records.purge_old_scan_records`; if the settings cannot be read it uses 30) in batches of 5,000 and at most 20 batches per run. Deleting a user removes their rows by cascade. **Phase 2 account deletion and data export must include this table.**

### `scan_corrections` (`backend/app/models/scan_correction.py`)

Whether the user changed the scan's reading when they saved an "Add it yourself" entry (`POST /wishlist/unverified`) pre-filled from it (#631). Written only by `app/ai/scan_corrections.py`. **No text column:** neither what the scan read nor what the user typed is stored, only three server-derived booleans.

| Column | Type | Notes |
|---|---|---|
| `id` | bigserial PK | |
| `scan_record_id` | UUID FK → `scan_records.id`, not null, `ON DELETE CASCADE` | the record the app named (`scan_reference.scan_record_id`), looked up for the session's user only; the user is reachable through it (no `user_id` column) |
| `candidate_index` | smallint, not null, ≥ 0 | which of the record's `candidates` the form was pre-filled from |
| `feature` | varchar(64), not null | copied from the record |
| `tier` | varchar(16), not null | copied from the record |
| `model` | varchar(64), not null | copied from the record |
| `title_edited` | boolean, not null | saved title differs from the pre-filled one after `normalise_title` (case, punctuation, spacing and a leading article are not edits) |
| `author_edited` | boolean, not null | saved author differs after `normalise_text` (the author string as read, however many authors it names) |
| `isbn_edited` | boolean, not null | saved ISBN differs as an ISBN-13 (hyphens, spaces, the ISBN-10 form are not edits); added, removed or changed is an edit. The form's ISBN starts empty for every result "Add it yourself" is offered for (enrichment keeps only provider-confirmed ISBNs, and no provider matched), so today this means the user typed one |
| `created_at` | timestamptz, not null, default `now()` | |

Constraints: `uq_scan_corrections_record_candidate (scan_record_id, candidate_index)` (a second save of the same candidate inserts nothing, `ON CONFLICT DO NOTHING`, and is not counted again); `ck_scan_corrections_index`. **Indexes:** the unique one (also serves the cascade); `ix_scan_corrections_created_at` for the purge.

**Writes:** after the entry is saved and its response built, in its own transaction: the insert and, when it inserted a row, the add to `scan_corrections_daily`, committed together. A reference to a record that does not exist, expired (`scan_records.retention_days`), belongs to another user or does not validate, and an index out of range, are ignored (nothing written, same response). Any failure is logged (error type only) and rolled back; the save never fails because of it.

**Volume and retention:** about 60 bytes per pre-filled save, at most one per (record, candidate). Rows go with their scan record (cascade) and are deleted 30 days after `created_at` by the ledger's daily run (`ledger.rollup_and_purge`, in batches of 5,000, at most 20 per run). **Phase 2 account deletion and data export must include this table.**

### `scan_corrections_daily` (`backend/app/models/scan_correction.py`)

Daily totals of `scan_corrections` per UTC day, feature and model (#631). No user column.

| Column | Type | Notes |
|---|---|---|
| `day` | date, PK | UTC day of the save |
| `feature` | varchar(64), PK | |
| `model` | varchar(64), PK | |
| `prefilled_saves` | bigint, not null, ≥ 0 | correction rows counted |
| `title_edited`, `author_edited`, `isbn_edited` | bigint, not null, ≥ 0 | rows with that flag set |
| `any_edited` | bigint, not null, ≥ 0 | rows with at least one flag set |

**Writes:** an upsert that adds 1 / 0 to each count (`INSERT … ON CONFLICT (day, feature, model) DO UPDATE SET prefilled_saves = prefilled_saves + excluded.prefilled_saves, …`) in the same transaction as the `scan_corrections` insert. **Why at write time, not after 30 days like `ai_usage`:** a correction row cascades with its scan record, which can be deleted well before 30 days (`scan_records.retention_days` can be as low as 1, the per-user cap evicts the oldest records, account deletion), so a 30-day rollup could lose rows; counting in the insert's transaction cannot lose or double-count one, and the daily run only deletes.

**Volume and retention:** tens of rows per day, under 100 bytes each. Kept 400 days; `ledger.rollup_and_purge` deletes older rows.

### `ai_spend_daily` (`backend/app/models/ai_spend.py`)

The service-wide daily AI cost circuit breaker (#531, `app/ai/breaker.py`). One row per UTC day, all tiers and features together. Standalone: no foreign keys, no user column, no content.

| Column | Type | Notes |
|---|---|---|
| `day` | date, PK | UTC day the calls were reserved on |
| `reserved_micros` | bigint, not null, default 0, ≥ 0 | sum of the cost estimates (`FeatureSpec.est_cost_ceiling_micros`) of calls reserved but not yet settled |
| `settled_micros` | bigint, not null, default 0, ≥ 0 | cost of finished calls: the priced cost of an answered call, the estimate of a `timeout`/`error`/cancelled call, nothing for a refunded call (provider unavailable or never reached) |
| `alerted_mask` | smallint, not null, default 0, ≥ 0 | bit 0/1/2 set once the 50/75/90 % spend alert was sent for the day |

The non-negative rule is one CHECK, `ck_ai_spend_daily_non_negative`.

**Writes:** only `app/ai/breaker.py`, on the request's session: the reservation upsert in `quota.reserve` (`INSERT … ON CONFLICT (day) DO UPDATE SET reserved_micros = reserved_micros + est`, with `WHERE reserved_micros + settled_micros + est <= ceiling` for Free; Premium has no condition); the give-back (`reserved_micros = greatest(reserved_micros - est, 0)`); the settle, executed with the `ai_usage` row (only when that insert returned a row, so a retried settle is not counted twice) and committed with the lease delete; and the alert claim (`UPDATE … SET alerted_mask = alerted_mask | bit WHERE alerted_mask & bit = 0`). The ceiling is the `ai_settings` key `breaker.free_daily_ceiling_micros`. No endpoint reads or writes the table directly.

**Volume and retention:** one row per day, under 60 bytes. Rows older than 90 days are deleted by the daily cleanup task (`app/services/token_cleanup.py`, calling `breaker.purge_old_spend`) in bounded batches.

### `ai_settings` (`backend/app/models/ai_settings.py`)

Server-side AI dials and kill switches ([architecture.md](architecture.md#book-identification--dedup-pipeline)). Standalone: no foreign keys, no user data.

| Column | Type | Notes |
|---|---|---|
| `key` | text, PK | one of the keys defined in `app/ai/settings.py`; other keys are ignored |
| `value` | JSONB, not null | bool, string or int depending on the key; validated on write and again on read |
| `updated_at` | timestamptz, not null, server default `now()` | set on every write |
| `updated_by` | text, nullable | `seed` for startup seeding, else the CLI's `--by` / `$USER` / `cli` |

**Writes:** only the startup seed (insert-if-missing) and the CLI `python -m app.ai.settings set`. No endpoint reads or writes it from a request.

**Volume and retention:** 20 known keys today (7 from #526, plus `scan.global_daily_limit` and the found and no-match allowances per tier from #528 and #577, plus `breaker.free_daily_ceiling_micros` from #531, plus the three `scan.enhanced.zero_result_*` keys from #533, plus `scan_records.enabled`, `scan_records.retention_days` and `scan_records.max_per_user` from #585, plus `scan.requests_per_user_daily` from #586), each under 300 bytes. The three monthly keys (`quota.free.monthly`, `quota.premium.monthly`, `quota.premium.enhanced_monthly`) are retired by #582: no longer seeded, refused by the CLI and ignored on read; migration 0018 (#594) deleted the rows seeded before. It does not grow with usage, so no retention job.

## Dedup strategy — why work_id, not ISBN

The dedup check lives in `backend/app/services/deduplication.py` and runs inside the `POST /scan` and `GET /books/search` response pipelines. Since #587 it is edition-aware, and it makes one query per response (`held_entries`), shared with `POST /wishlist`'s 409:

```python
# Pseudocode of deduplication.held_entries + ownership_of
rows = db.exec(
    select(UserBook.book_id, UserBook.edition_id,
           Book.open_library_work_id, Book.google_books_id,
           Edition.isbn_13, Edition.isbn_10)
    .join(Book, Book.id == UserBook.book_id)
    .outerjoin(Edition, Edition.id == UserBook.edition_id)
    .where(UserBook.user_id == user_id,
           or_(Book.open_library_work_id.in_(work_ids),
               Book.google_books_id.in_(volume_ids),
               Edition.isbn_13.in_(isbn_13s), Edition.isbn_10.in_(isbn_10s)))
    .order_by(UserBook.edition_id.is_(None))  # entries with an edition first
)
# per candidate: the rows of its work ID, volume ID or ISBN (editions[0],
# ISBN-10 converted to 13). Any row with that ISBN -> owned_same_edition;
# else any row with an ISBN -> owned_other_edition; else any row ->
# owned_edition_unknown; no row -> not_owned.
```

**Why not ISBN:**
- A single work (*Dune*) has dozens of editions — hardcovers, paperbacks, international translations, special anniversary reprints, audiobooks, each with its own ISBN.
- ISBN-based dedup would let a user wishlist 14 copies of *Dune* and flag none of them as duplicates.
- Users think in terms of the **work** (*Dune*), not the **edition** (*Dune, 40th Anniversary Hardcover, Ace 2005*).

**Why the Google Books fallback:**
- Open Library doesn't always have a `work_id` (incomplete coverage on newer / self-published books).
- Google Books `volume_id` is still work-level (one per title+author), so it's a reasonable second choice.
- If neither is available, the candidate is returned with `already_in_library=False` and the user sees a duplicate — rare enough to accept.

**When dedup is actually applied:**
- On `POST /scan` and `GET /books/search` the candidates are marked (`ownership`, plus `already_in_library` / `in_library`) but still returned. The frontend's `BookCandidatePicker` shows the edition-level note and does not let the user tick a book they already have.
- On `POST /wishlist` a book the user already has (an entry for the `Book`, or an entry holding an edition with the same ISBN) is refused with `409` and the ownership code, and nothing is written (#587). The unique constraint `uq_user_books_user_book` on `(user_id, book_id)` remains the database-level enforcement.

## Migration workflow

Migrations are managed with Alembic and live in `backend/alembic/versions/`:

- `0001_initial_schema.py` — creates `users`, `books`, `editions`, `user_books`
- `0002_refresh_tokens.py` — adds `refresh_tokens` table
- `0003_enhanced_scan_credits.py` — adds `users.enhanced_scan_credits`
- `0004_restrict_catalog_deletes.py` — `user_books.book_id` and `editions.book_id` FKs become `ON DELETE RESTRICT`
- `0005_scan_usage_daily.py` — adds `scan_usage_daily` table (daily scan caps)
- `0006_ai_settings.py` — adds `ai_settings` table (DB-backed AI dials and kill switches; rows are seeded at app startup, not by the migration)
- `0007_ai_quota.py` — adds `ai_quota` (per-user daily/monthly AI allowance counters; month rows are legacy since #582) and `ai_inflight` (one call in flight per user) tables; additive, the downgrade drops both
- `0008_ai_usage.py` — adds `ai_usage` (one row per paid AI provider call) and `ai_usage_daily` (daily rollup) tables; additive, the downgrade drops both
- `0009_ai_spend_daily.py` — adds `ai_spend_daily` (service-wide daily AI spend for the cost circuit breaker); additive, the downgrade drops it
- `0010_ai_enhanced_zero_results.py` — adds `ai_enhanced_zero_results` (uncharged empty enhanced scans, #533; `warned` boolean, default false, marks the row that sent the threshold warning) with the `(user_id, created_at)` index; additive, the downgrade drops it
- `0011_scan_records.py` — adds `scan_records` (one record per user and photo fingerprint, #585) with the `(user_id, fingerprint)` unique constraint and the `(user_id, created_at)` and `(created_at)` indexes; additive, the downgrade drops it. The `ai_usage` status check already allowed `cache_hit` (0008), so it is unchanged
- `0012_ai_inflight_units_taken.py` — adds `daily_taken`, `monthly_taken` and `enhanced_taken` (boolean, not null, server default false) to `ai_inflight`, so a unit whose reservation commit lost its acknowledgement can be given back at most once (#590); expand-only, the downgrade drops the three columns. Since #582 only `daily_taken` is set; `monthly_taken` and `enhanced_taken` were dropped by 0018
- `0013_ai_usage_malformed_status.py` — widens the status CHECK of `ai_usage` and `ai_usage_daily` (`ck_ai_usage_status`, `ck_ai_usage_daily_status`) to allow `malformed` (#572); additive. Each widened CHECK is added `NOT VALID` under a 5 s `lock_timeout`, the swap is committed, then each is validated in its own transaction (`VALIDATE CONSTRAINT`, which does not block reads or writes), so no ACCESS EXCLUSIVE lock is held while rows are scanned. The downgrade is deliberately a no-op: both widened CHECKs stay, because any narrower CHECK (even `NOT VALID`) would break the old code on the kept `malformed` rows (the rollup into `ai_usage_daily`, and deleting their user, which sets `user_id` to NULL). The old code never writes `malformed`.
- `0014_ai_usage_attempt.py` — adds the nullable `smallint` column `ai_usage.attempt` (#603); expand only. Existing rows stay NULL (unknown); no default, so only the catalog changes (no rewrite), under a 5 s `lock_timeout`. The downgrade drops the column; roll the app code back first, since the #603 ledger insert always names `attempt`.
- `0015_books_google_books_id_unique.py` — merges duplicate `books` rows sharing a `google_books_id`, then creates the partial unique index `uq_books_google_books_id` on `books (google_books_id) WHERE google_books_id IS NOT NULL` (#613, owner decision option A). Per volume the oldest row (`created_at`, then `id`) is kept. `user_books` on the newer rows move to it; where a user has more than one entry for the merged book (`uq_user_books_user_book`), the entry already on the kept row survives (else the user's oldest), its NULL fields (`edition_id`, `notes`, `rating`, the `*_at` timestamps) take the first non-NULL value from the others (oldest first), its `status` becomes the most advanced among the merged entries (wishlisted < purchased < reading < read), and the others are hard-deleted (`user_books` has no `deleted_at`). `editions` move to the kept row; if the kept row has no `open_library_work_id`, the oldest duplicate's moves to it; the duplicates are then deleted. Counts are logged, with at most 20 deleted book IDs per batch. Batches of 500 kept books with set-based SQL bound statement size only: it is all one transaction. More than 10,000 duplicate rows fails the upgrade with a message to merge by hand. Before reading the duplicates it takes `LOCK TABLE books, user_books, editions IN SHARE ROW EXCLUSIVE MODE` (writes to the three tables wait, reads run; 5 s `lock_timeout`, reset to the default at the end of the revision), held until the whole `alembic upgrade` run commits, so no write can race the merge, and builds a plain `CREATE UNIQUE INDEX`, not `CONCURRENTLY`, so no duplicate can slip in between the merge and the build. Offline `alembic upgrade --sql` is refused (the merge reads rows). The downgrade drops the index only; merged rows are not restored.
- `0017_scan_corrections.py` — adds `scan_corrections` (correction flags per scan record and candidate, #631; FK `scan_records` `ON DELETE CASCADE`, unique `(scan_record_id, candidate_index)`, `created_at` index) and `scan_corrections_daily` (daily totals, no user); expand only, the downgrade drops only these two tables
- `0018_drop_retired_monthly_quota.py` — contract step for #582 (#594): deletes the `ai_settings` rows `quota.free.monthly`, `quota.premium.monthly` and `quota.premium.enhanced_monthly` (only those keys) and drops `ai_inflight.monthly_taken` and `ai_inflight.enhanced_taken`. `ai_quota` month rows are left to age out through the 13-month retention. The downgrade recreates the two columns as 0012 made them (boolean, not null, default false) but cannot restore the deleted settings rows (the release before #582 re-seeds missing quota keys at boot).

### Writing a new migration

```bash
cd backend
source .venv/bin/activate

# Make your model change first (edit app/models/*.py)

# Autogenerate a migration skeleton
alembic revision --autogenerate -m "short description"

# Review the generated file — Alembic's autogen is imperfect
# (misses check constraints, reorders things, can generate no-ops)
$EDITOR alembic/versions/<new-file>.py

# Apply it
alembic upgrade head
```

### Check-constraint gotcha

Alembic autogen **does not detect `CheckConstraint` changes** well. When you modify or add a check constraint (e.g. a new status value, a different rating range), you typically need to:

1. Add an explicit `op.drop_constraint(...)` for the old one
2. Add an explicit `op.create_check_constraint(...)` for the new one

The existing `VALID_STATUSES` / `VALID_FORMATS` tuples in the model files are the source of truth — autogen will read them on future generations, but won't produce the drop+create dance for in-place edits.

### Rolling back

```bash
alembic downgrade -1           # one step back
alembic downgrade <revision>   # to a specific revision
alembic downgrade base         # nuke everything (dev only)
```

**Production caveat:** Render runs `alembic upgrade head` as `preDeployCommand` on every deploy (see `render.yaml`). A migration that fails there blocks the deploy. Test locally against a copy of the prod schema before merging migration PRs.

## Rules referenced

- Rule #7: SQLAlchemy ORM only — no raw SQL. The `_is_in_library` dedup check, the `/auth/refresh` jti lookup, and all CRUD paths use `select(...)` constructs, not text SQL.

# Backend Conventions

## Stack
FastAPI · SQLAlchemy (async) · asyncpg · Alembic · pydantic-settings · httpx · slowapi

## Running Locally

```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env   # fill in your values
psql -d bookshelf -c "CREATE ROLE bookshelf_app LOGIN PASSWORD 'dev'"   # once per cluster; migration 0028 needs it (#696)
alembic upgrade head
uvicorn app.main:app --reload
```

## Key Patterns

See [~/.claude/standards/code-style.md](~/.claude/standards/code-style.md) for universal Python conventions (pydantic-settings config, ORM-only, Pydantic schemas on all routes).

### Routes
- All routes (Phase 3+) require `get_current_user` FastAPI dependency
- Pydantic schemas for every request and response body — no untyped dicts
- Max image upload: 5MB — enforce before reading body; also validate file extension, not just MIME type

### Rate Limiting
Every endpoint is rate-limited via slowapi. The `limiter` instance lives in `app/main.py` and is imported by each router. Authenticated routes key by user ID; unauthenticated routes key by IP.

| Endpoint | Limit | Config key |
|---|---|---|
| `POST /auth/google`, `POST /auth/refresh`, `POST /auth/logout`, `POST /auth/logout-all` | 5/min | `rate_limit_auth` |
| `POST /scan` | 10/min | `rate_limit_scan` |
| `GET /books/search` | 30/min | `rate_limit_books_search` |
| `POST /wishlist`, `POST /wishlist/unverified`, `POST /purchased`, `PATCH`, `DELETE /user-books/*`, `POST /user-books/{id}/restore`, `PATCH /users/me/preferences`, `POST` / `DELETE /users/me/ai-consent`, `POST /account/export` | 60/min | `rate_limit_writes` |
| `GET /user-books`, `GET /user-books/trash`, `GET /auth/me` | 120/min | `rate_limit_reads` |

All limits are env-var overridable via `Settings` in `app/core/config.py`.

### Auth
- JWT RS256 only — reject all other algorithms including `none`
- Access tokens live 15 minutes (`jwt_access_ttl_minutes`). Every access and refresh token carries `tv` = `users.token_version`; `get_current_user` compares it with the row it already loads (no cache, no extra query) → 401 `token_revoked`. Until the legacy path is removed (#735; it accepts nothing after 2026-11-03) a token with no `tv` (pre-#689) is legacy: access → 401 `token_outdated`, refresh → read as `tv = 0` (#725, `LEGACY_TOKEN_VERSION`; removal tracked in #735). To revoke a user's sessions, call `app/auth/sessions.py:revoke_all_sessions(db, user)` (bumps the version and revokes all refresh rows; the caller commits) — never bump or delete tokens by hand. A sign-in issues its token pair with `app/auth/sessions.py:issue_session_tokens(db, user)` (adds the new family's first row and commits; used by `/auth/google` and the dev-only test-login, #844); the primitives are `create_access_token(id, email, user.token_version)` / `create_refresh_token(id, user.token_version)` (#689)
- Refresh tokens form families (#690, `app/auth/refresh_family.py`): a sign-in's first row comes from `new_family_row(...)`, never a bare `RefreshToken(...)`; `/auth/refresh` rotates into `successor_row(...)`. A rotated token replayed after `REFRESH_REUSE_GRACE_SECONDS` (30 s), a displaced token, or a second retry of one token is reuse → `revoke_all_sessions`, 401 `refresh_reused`, Sentry warning with user id and family id only. A legacy (no `tv`) refresh token rotates without `used_at`, so its replay is a plain 401
- Allowlist check via `ALLOWED_EMAILS` env var after Google auth
- Users are matched by Google `sub` (`users.google_id`) via `app/auth/identity.py:resolve_google_user`, never by email; an email owned by another subject (compared with `lower()`) → 409 `account_conflict`; `ALLOWED_EMAILS` also ignores case
- `/auth/test-login` exists only in development/test; hardened settings with `TEST_AUTH_SECRET` set fail boot
- Logs and Sentry: log user IDs, never emails, allowlist entries, tokens (access, refresh, ID tokens), Google/Apple `sub` values or `Authorization` headers. `tests/security/test_log_hygiene_flows.py` (helpers in `tests/_log_hygiene.py`) drives the real auth, library, scan and error flows and fails on any such value in a log record or Sentry event. A story that adds a flow handling user data (e.g. account deletion) must add it there.

### AI scan consent (#1023)
- Consent to send scan photos to the AI provider lives on the user row (`users.ai_consent_at`, `ai_consent_withdrawn_at`; `User.ai_consent` says whether it stands). Only `app/services/ai_consent.py` (`grant`, `withdraw`) sets them, from the server's clock, through `POST` / `DELETE /users/me/ai-consent` (row re-read under `FOR UPDATE`). Never take a timestamp or a consent flag from a request.
- `POST /scan` calls `ai_consent.require_for_scan(user)` right after the kill switches and before the request count, the bot check, the upload, the saved-answer lookup, the allowance and the provider, so a refusal (`403 ai_consent_required`) costs nothing. It refuses only while `AI_CONSENT_REQUIRED` is on (default off: rollout A). A new path that sends a user's photo to a model must make the same check first.

### Catalog/user boundary (#602, #611, #682)
- `books` / `editions` are the **shared catalog**. Only `WishlistService` (`POST /wishlist`) creates rows, only from provider data the server signed (`catalog_token`), and no request ever updates an existing catalog row. Client input never reaches these tables.
- Anything a user wants to change about *their* copy goes in their own row: `user_books.title_override`, `author_override`, `cover_url_override`, set or cleared (`null`) through `PATCH /user-books/{id}`. Library reads (`UserBookRead`) show the override in `book.title` / `book.author` / `book.cover_url` and the catalog value otherwise, and report the user's values in `overrides` so the app can offer "reset to catalog". Overrides use the catalog's limits (500 / 500 / 2048), refuse control characters and Unicode format characters (the cover also whitespace), and the cover goes through `validate_safe_url(..., resolve=False)` (https; no `localhost` or private/reserved IP-literal host; no DNS lookup, since the server never fetches it). An unverified entry (#616) takes none (422 `overrides_not_supported`; check constraint `ck_user_books_overrides_catalog_only`).
- Every new catalog row records provenance: `books.source` (`google_books` when a Google Books volume matched, else `open_library`, else `scan` for rows from before #602; `app/services/catalog.py:source_of`) and `books.fetched_at`. A new catalog write path must set both.
- Merged duplicates keep their row with `books.merged_into_id` pointing at the survivor (self-FK, `ON DELETE RESTRICT`, always one hop). Reads follow it (`_user_book_query` loads `Book.merged_into`, `catalog.effective_book`; `held_entries` counts an entry on any row of a merged work as the survivor's and matches it on any of the work's IDs, so scan / search and `POST /wishlist` agree). Entries on merged rows can exist: `POST /purchased` promotes one (looking up `book_id IN (survivor, rows merged into it)`) rather than add a second entry, and the #686 edition check runs against the survivor. Writes never target a merged row: `POST /wishlist` and `POST /purchased` redirect to the survivor (`catalog.resolve_survivor`), so a stale `book_id` or provider ID still adds the same work once. There is no runtime merge code; the only merge so far (migration 0015) deleted its duplicates. A future merge sets `merged_into_id` (re-pointing earlier merges into the duplicate) and moves the duplicate's `user_books` / `editions` to the survivor. See `app/services/catalog.py`.

### Soft delete and the trash (#683)
- Deleting a library entry sets `user_books.deleted_at` (and `user_custom_books.deleted_at` for an unverified entry); nothing is hard-deleted except by the daily trash purge (`app/services/trash.py`) after `TRASH_RETENTION_DAYS` (30), by `DELETE`'s same-book eviction (`trash.evict_same_book`: replaces older trashed copies of the same book that hold no user data, `UserBook.has_no_user_data()`; copies with notes, rating, edition, overrides or a status beyond wishlisted stay; same work via `catalog.of_work`, or the unverified duplicate key `unverified_books.same_book`; add a column to `has_no_user_data()` when you add a user-set column to `user_books`), by the per-user trash cap (`TRASH_MAX_ENTRIES_PER_USER`, 1,000, a backstop: `DELETE` evicts the oldest beyond it) and by account deletion.
- Lock order for library writes: the user's row (`lock_user`) → `user_books` → `user_custom_books`. `DELETE`, restore, `POST /purchased` and `POST /wishlist` take the user lock first; row reads before a write use `with_for_update` and keep the `live()` filter. A response is read back before the commit (`_reread_and_commit`), never after.
- **Every query on `UserBook` or `UserCustomBook` must exclude trashed rows** with `UserBook.live()` / `UserCustomBook.live()` (`deleted_at IS NULL`), unless it is the trash list, restore or the purge. Library reads go through `_user_book_query` in `app/api/user_books.py`, which applies it (`trashed=True` for the trash). This includes counts, caps, duplicate checks (`held_entries`, `UnverifiedBookService`), lookups before a write (`POST /purchased`) and any future stats path. The account export (#694) is the one exception: it is the user's copy of everything held, so it includes trashed entries with their `deleted_at` (`app/services/account_export.py`). `tests/unit/test_soft_delete_trash.py` checks the existing queries; a new query needs its own test that a trashed entry is invisible.
- Uniqueness of `(user_id, book_id)` holds among live rows only (`uq_user_books_user_book_live`, partial). A restore that would make a second live entry of a work is `409 restore_conflict`.
- Account deletion (#291) must delete trashed rows at once, not leave them to the purge.

### Entry versions: ETag / If-Match (#685)
- `user_books` and `user_custom_books` have `version` (`VersionedMixin` in `app/models/_mixins.py`, SQLAlchemy `version_id_col`): the ORM bumps it on every UPDATE and writes `UPDATE ... WHERE id = ? AND version = ?`. Change these rows through the ORM only; a bulk `update(UserBook)` does not bump the version.
- The user-books router uses `VersionETagRoute` (`app/api/_concurrency.py`): every single-entry response gets `ETag: "<version>"` from its body, so a new endpoint returning one `UserBookRead` gets it for free.
- A write to one entry takes `if_match: Annotated[str | None, Header()] = None`, calls `parse_if_match` first (400 `invalid_if_match`, before any query), then, after the live, user-scoped lookup under `FOR UPDATE` (so another user's entry stays 404), `require_version(...)` (412 `version_mismatch` with `current_version`; a missing header is accepted and logged with the user id only). Wrap the flush and commit in `stale_write_as_412(db)`. A new single-entry `PATCH` / `PUT` / `DELETE` must do all three.
- The version is server-owned: never accept it in a request body.

### Idempotency-Key (#685)
- Every library and account write honours an optional `Idempotency-Key` (`app/api/_idempotency.py`, store in `app/services/idempotency.py`, table `idempotency_keys`). A keyed endpoint has `@idempotent` directly under `@limiter.limit(...)` (so a replay counts against the limit and a `429` never claims a key), takes `request`, `current_user` and `db` by those names, and lives on a router whose route class includes `IdempotencyRoute` (it stores the serialised response). On the user-books router that is `_LibraryRoute(IdempotencyRoute, VersionETagRoute)`: idempotency first in the bases, so the stored response carries the `ETag`. A new write route on these routers needs both; any other new write route must be keyed or listed in `NOT_KEYED` with a reason (`tests/security/test_idempotency_coverage.py`).
- The claim is its own committed transaction before the endpoint runs, on the request's session (no second pool connection); it never rolls back, so the already-loaded `current_user` stays usable. From the claim on, a `before_commit` listener marks the row `committed` in the endpoint's own transaction, so a key is never released or taken over once its write is durable; a repeat then gets `409 idempotency_outcome_unknown`. An endpoint's `HTTPException` 4xx is stored and replayed (after a rollback of what it left uncommitted); a 5xx, a `429` or any other exception releases the key only if nothing was committed. Endpoints need nothing else, but **read everything the response needs before the commit** (`flush`, `refresh`, then `commit`, as `WishlistService.add` and `UnverifiedBookService.add` do): a failure after the commit answers 5xx for a write that is applied, and the retry then gets the outcome-unknown 409 instead of the response.
- Never log a key or a stored body (user id only); the header is in the Sentry scrubber's denylist. There is no kill switch: the app retries keyed POST / PATCH, so ignoring the header would bring double writes back. Tests: the decorator against `tests/_idempotency_fake.py`, the store against a scripted session, everything else on Postgres (`tests/integration/test_idempotency_db.py`).

### Library audit log (#684)
- Every library mutation writes one `library_events` row per entry it changes, **in its own transaction, before its commit**: `library_events.record_created(db, entry)` for a new entry, `before = library_events.snapshot(entry)` then `record_change(db, entry, before[, event_type])` for a change (writes nothing when nothing changed), and `record_purged` for hard deletes (already done by `trash._delete_entries`, the only path that hard-deletes `user_books`). A new mutation path must call one of them; `tests/integration/test_library_events_db.py` covers each endpoint.
- `changed_fields` holds `{field: {old, new}}` of structured columns only. Free text (`library_events.FREE_TEXT_FIELDS`: notes and the overrides) is recorded as `"changed"`. When you add a user-set text column to `user_books`, add it to `TRACKED_FIELDS` and `FREE_TEXT_FIELDS` (`tests/unit/test_library_events.py` fails otherwise).
- Append-only: only `app/services/library_events.py` may import `LibraryEvent` to write (it also has the one read, `history`, for the account export), and only `app/services/library_events_retention.py` (180 days, `LIBRARY_EVENTS_RETENTION_DAYS`, batched, advisory lock) deletes. `tests/security/test_library_events_append_only.py` fails on any other update or delete; the app role has no `UPDATE` on the table (migration 0030).

### Object storage, email and the account export (#694)
- **Object storage** goes through `app/services/object_storage.py` (Supabase Storage REST over httpx): `ObjectStorage.upload` (overwrites, `x-upsert`), `signed_url(path, expires_in, download_name=)`, `delete(paths)` (bulk; missing objects are fine) and `list(prefix)`. Build it with `object_storage.from_settings()`, which is `None` while `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` are unset: treat `None` as the feature's kill switch. It is generic (the debug photos of #874 reuse it): pass the bucket, keep the meaning of objects in the caller. Buckets are private; hand out signed URLs only, and never log one (it is a credential), nor the service-role key.
- **Email** goes through `app/services/email.py:send(Message, idempotency_key=...)` (Resend): from `Bookshelf <EMAIL_FROM>`, `Reply-To: EMAIL_REPLY_TO`, HTML and text bodies, Resend's `Idempotency-Key` so a retried job sends once. `RESEND_API_KEY` unset = `email.configured()` is false. Never log the recipient, the subject or a body. Localised strings live in `app/email_locales/<locale>.json` (one file per app locale, a namespace per email; `tests/unit/test_export_email.py` checks every locale has every key); escape every value put into the HTML.
- Both clients use `app/services/_http_retry.py:send_with_retries` (3 tries, 0.5 s / 1 s backoff on a transport error, timeout, 429 or 5xx; anything else final) and a 15 s timeout, and raise an error carrying the status only. Tests use `httpx.MockTransport` (the clients take `transport=`); never the real services.
- **Background work that must survive a restart** is a row in the database plus a worker, as the account export does (`app/services/account_export_jobs.py`): the request inserts a `pending` row and calls `wake()`; `run_worker` (started in the lifespan next to `cleanup_expired_tokens`) runs a pass on a wake-up and every 5 minutes, claiming rows with `FOR UPDATE SKIP LOCKED` and a lease (`claimed_at`, 10 minutes) so two instances never take the same row and a dead run is taken over; at most 3 attempts. Every write after the claim is fenced on the row's status and this attempt's `claimed_at` (`Claim`, `_fenced_update`): an attempt whose lease was taken over changes nothing. A side effect that cannot be repeated safely (the email) is made idempotent first: everything it needs is committed before it runs (`emailing`, `delivery_url`), the retry rebuilds the identical request, and the provider's idempotency key dedupes it. Trust a provider's "key already used" answer only when your own record says an earlier request may have been accepted (`email_accepted_at`). The purge of the export rows and files is `app/services/account_export_purge.py`. Never overwrite an object whose age a sweep reads: Supabase keeps the first `created_at` on an upsert, so write a new path and delete the old one. Read a multi-table document from one snapshot (`account_export.build_snapshot`: read-only `REPEATABLE READ`, ended before any HTTP call). Its sessions come from `JobSessionLocal`. The daily cleanup purges the rows and the stored objects.
- **Account export coverage.** `app/services/account_export.py` lists every user table it exports (`SECTIONS`, with each column exported or left out with a reason) or leaves out (`EXCLUDED_TABLES`). `tests/unit/test_account_export_document.py` derives the user tables from the models (`tests/_user_tables.py`: a foreign key to `users`, directly or through another such table) and fails on a new table or column nobody decided about. Add your table or column there; account deletion (#291) should use the same enumeration.

### Database
- The app connects as **`bookshelf_app`** (#696) — a least-privilege login role with `SELECT`, `INSERT`, `UPDATE`, `DELETE` on all app tables and `USAGE` on sequences. It cannot run DDL. Alembic migrations run as the **owner** (`postgres` on Supabase): `alembic/env.py` uses `settings.async_migration_database_url`, i.e. `MIGRATION_DATABASE_URL` when set, else `DATABASE_URL` (local dev and CI connect as the owner, so they leave it unset). Never point Alembic at the app engine or `settings.async_database_url`.
- Every new table's migration enables row level security (`op.execute('ALTER TABLE "<name>" ENABLE ROW LEVEL SECURITY')`), with no policies. The app connects as `bookshelf_app` which has `BYPASSRLS`, so it behaves like the owner for row visibility. `tests/integration/test_rls_enabled_db.py` fails otherwise (#677)
- Never `GRANT` anything to `anon` or `authenticated`. Migration 0023 revoked their table and sequence privileges, including the migrating role's default privileges, so a new table needs no extra step (#704)
- Migration 0028 set default privileges on the owner role so every future table / sequence created by the owner automatically inherits DML grants for `bookshelf_app`. No per-table grant is needed in new migrations.
- **Statement timeout**: the app engine sets `statement_timeout = STATEMENT_TIMEOUT_MS` (default 5 s) as an asyncpg startup parameter (`connect_args={"server_settings": ...}`) and again with `SET` on every pool checkout (`app/core/database.py`). Never put a libpq `?options=` query on `DATABASE_URL`: asyncpg rejects it. Through SQLAlchemy a timeout surfaces as `sqlalchemy.exc.DBAPIError` with SQLSTATE `57014` on `exc.orig`; `_query_canceled_handler` in `app/main.py` turns that into `HTTP 503 {"detail": "db_timeout"}` + `Retry-After`, logs the path, sends one Sentry warning (fingerprint `db-statement-timeout`, route-template tag only) and re-raises every other `DBAPIError`. Background jobs that need more time open their sessions from `JobSessionLocal` (`app/core/database.py`), which runs `SET LOCAL statement_timeout = STATEMENT_TIMEOUT_JOB_MS` (default 60 s) at the start of every transaction; the daily purges in `app/services/token_cleanup.py` do. Never raise the request timeout for a job.
- **Role check at startup**: `log_db_role()` logs `db_role current_user=<role>` once from the lifespan, as a warning when a hardened environment is not on `bookshelf_app`. After a restore, re-apply the role's grants with `scripts/db/app_role_grants.sql` (`docs/backups.md`); every table in `public` must have DML for the role, except `UPDATE` on the append-only `library_events` (#684) (`tests/integration/test_db_role.py`).

## Commands

```bash
# Tests
pytest tests/ --cov=app --cov-fail-under=80

# Lint + format
ruff check .
black --check .
# Every `# noqa: CODE` / `# type: ignore[code]` in app/ ends with ` - <reason>`
# (enforced by tests/unit/test_suppression_reasons.py; ruff RUF100 drops stale ones)

# Dead code (vulture, #813; config and whitelist in pyproject.toml [tool.vulture])
vulture app --min-confidence 60

# Migrations
alembic revision --autogenerate -m "description"
alembic upgrade head
```

### Migrations: expand/contract (#687)
Change the schema in two releases, never one:
1. **Expand.** A migration that only adds: new columns (nullable or with a server default), new tables, new indexes. Ship code that works with both the old and the new shape.
2. **Contract.** Once no running release uses the old shape, a later migration removes it (drop column or table, tighten nullability, change a type). Examples: #582 then #594 (migration 0018) and #528/#533 then #540 (migration 0019).

Render runs `alembic upgrade head` before the new code takes traffic, so old code is live while a migration runs; a destructive step in the same release as the code change breaks the overlap.

**CI guard.** The `Migration guard` workflow (`.github/workflows/migration-guard.yml`, scanner `scripts/migration_guard/check_destructive_migrations.py`) runs on every pull request to `dev` and again whenever a label is added or removed. It scans the migration files the PR adds, modifies or renames (`git diff --no-renames --diff-filter=AM`, so a renumbered file is scanned) under `backend/alembic/versions/`. It reads the code with `ast`: the body of `upgrade()` plus any module-level helper it calls, directly or through other helpers (the `downgrade()` is ignored).

Detected (fails the PR without the label):
- `drop_table`, `drop_column`, `rename_table`, on `op` (also an alias from `from alembic import op as X`) and on the `batch_op` of `with op.batch_alter_table(...)`
- `alter_column` with `new_column_name`, `type_=` (any type change counts; narrowing cannot be detected reliably) or `nullable=False`
- `.execute(...)` on any receiver (`op`, `op.get_bind()`, `conn`), and calls to a followed helper, whose string arguments contain DROP TABLE/COLUMN/SCHEMA/TYPE/VIEW, TRUNCATE, DELETE FROM (not `ON DELETE CASCADE`), `ALTER TABLE ... RENAME` or `ALTER ... TYPE` within one statement (case-insensitive), or that build a `delete(...)` construct. Module-level string constants, local `name = "..."` assignments inside the scanned functions, `sa.text(...)`, `+` concatenation and f-strings (interpolated parts count as a placeholder) are resolved
- a migration with no top-level `upgrade()` (fails closed, since it cannot be checked)

Not detected: dropping an index or a constraint (allowed, no data is lost); SQL built at run time from values the file does not define (imports, function results, loops over data); destructive steps in code the file imports from another module; `SET NOT NULL` written as raw SQL; `.execute(...)` of a statement object built elsewhere. `import alembic; alembic.op.X(...)`, `from alembic.op import X` and SQL assembled with calls such as `" ".join([...])` are not understood. Review these by hand.

The guard's own files (`.github/workflows/migration-guard.yml`, `scripts/migration_guard/`) are listed in `.github/CODEOWNERS`, so changing them needs the owner's review. The `migration-guard` check is listed in the required status checks of `.github/rulesets/protect-dev.json` and `protect-main.json`; the owner must apply both rulesets in GitHub settings, otherwise a failing guard does not block the merge. The workflow also triggers on PRs into `main` and passes at once there ("Release PR: migrations were already scanned on their feature PRs into dev"), so the required check reports on dev to main release PRs.

**Label process.** A PR that trips the guard needs the `destructive-migration-approved` label, which only the owner applies; contributors and agents must not add it. Until launch (the app is not live, decision 0) a destructive change may be a single migration with the label instead of a two-release expand/contract; after launch, use expand/contract. Run the scanner locally with `python3 scripts/migration_guard/check_destructive_migrations.py backend/alembic/versions/<file>.py`.

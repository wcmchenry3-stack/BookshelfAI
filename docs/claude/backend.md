# Backend Conventions

## Stack
FastAPI · SQLAlchemy (async) · asyncpg · Alembic · pydantic-settings · httpx · slowapi

## Running Locally

```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env   # fill in your values
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
| `POST /wishlist`, `POST /wishlist/unverified`, `POST /purchased`, `PATCH`, `DELETE /user-books/*`, `POST /user-books/{id}/restore` | 60/min | `rate_limit_writes` |
| `GET /user-books`, `GET /user-books/trash`, `GET /auth/me` | 120/min | `rate_limit_reads` |

All limits are env-var overridable via `Settings` in `app/core/config.py`.

### Auth
- JWT RS256 only — reject all other algorithms including `none`
- Access tokens live 15 minutes (`jwt_access_ttl_minutes`). Every access and refresh token carries `tv` = `users.token_version`; `get_current_user` compares it with the row it already loads (no cache, no extra query) → 401 `token_revoked`. Until the legacy path is removed (#735; it accepts nothing after 2026-11-03) a token with no `tv` (pre-#689) is legacy: access → 401 `token_outdated`, refresh → read as `tv = 0` (#725, `LEGACY_TOKEN_VERSION`; removal tracked in #735). To revoke a user's sessions, call `app/auth/sessions.py:revoke_all_sessions(db, user)` (bumps the version and revokes all refresh rows; the caller commits) — never bump or delete tokens by hand. Issue tokens with `create_access_token(id, email, user.token_version)` / `create_refresh_token(id, user.token_version)` (#689)
- Refresh tokens form families (#690, `app/auth/refresh_family.py`): a sign-in's first row comes from `new_family_row(...)`, never a bare `RefreshToken(...)`; `/auth/refresh` rotates into `successor_row(...)`. A rotated token replayed after `REFRESH_REUSE_GRACE_SECONDS` (30 s), a displaced token, or a second retry of one token is reuse → `revoke_all_sessions`, 401 `refresh_reused`, Sentry warning with user id and family id only. A legacy (no `tv`) refresh token rotates without `used_at`, so its replay is a plain 401
- Allowlist check via `ALLOWED_EMAILS` env var after Google auth
- Users are matched by Google `sub` (`users.google_id`) via `app/auth/identity.py:resolve_google_user`, never by email; an email owned by another subject (compared with `lower()`) → 409 `account_conflict`; `ALLOWED_EMAILS` also ignores case
- `/auth/test-login` exists only in development/test; hardened settings with `TEST_AUTH_SECRET` set fail boot
- Logs and Sentry: log user IDs, never emails, allowlist entries, tokens (access, refresh, ID tokens), Google/Apple `sub` values or `Authorization` headers. `tests/security/test_log_hygiene_flows.py` (helpers in `tests/_log_hygiene.py`) drives the real auth, library, scan and error flows and fails on any such value in a log record or Sentry event. A story that adds a flow handling user data (e.g. account deletion) must add it there.

### Catalog/user boundary (#602, #611, #682)
- `books` / `editions` are the **shared catalog**. Only `WishlistService` (`POST /wishlist`) creates rows, only from provider data the server signed (`catalog_token`), and no request ever updates an existing catalog row. Client input never reaches these tables.
- Anything a user wants to change about *their* copy goes in their own row: `user_books.title_override`, `author_override`, `cover_url_override`, set or cleared (`null`) through `PATCH /user-books/{id}`. Library reads (`UserBookRead`) show the override in `book.title` / `book.author` / `book.cover_url` and the catalog value otherwise, and report the user's values in `overrides` so the app can offer "reset to catalog". Overrides use the catalog's limits (500 / 500 / 2048), refuse control characters and Unicode format characters (the cover also whitespace), and the cover goes through `validate_safe_url(..., resolve=False)` (https; no `localhost` or private/reserved IP-literal host; no DNS lookup, since the server never fetches it). An unverified entry (#616) takes none (422 `overrides_not_supported`; check constraint `ck_user_books_overrides_catalog_only`).
- Every new catalog row records provenance: `books.source` (`google_books` when a Google Books volume matched, else `open_library`, else `scan` for rows from before #602; `app/services/catalog.py:source_of`) and `books.fetched_at`. A new catalog write path must set both.
- Merged duplicates keep their row with `books.merged_into_id` pointing at the survivor (self-FK, `ON DELETE RESTRICT`, always one hop). Reads follow it (`_user_book_query` loads `Book.merged_into`, `catalog.effective_book`; `held_entries` counts an entry on any row of a merged work as the survivor's and matches it on any of the work's IDs, so scan / search and `POST /wishlist` agree). Entries on merged rows can exist: `POST /purchased` promotes one (looking up `book_id IN (survivor, rows merged into it)`) rather than add a second entry, and the #686 edition check runs against the survivor. Writes never target a merged row: `POST /wishlist` and `POST /purchased` redirect to the survivor (`catalog.resolve_survivor`), so a stale `book_id` or provider ID still adds the same work once. There is no runtime merge code; the only merge so far (migration 0015) deleted its duplicates. A future merge sets `merged_into_id` (re-pointing earlier merges into the duplicate) and moves the duplicate's `user_books` / `editions` to the survivor. See `app/services/catalog.py`.

### Soft delete and the trash (#683)
- Deleting a library entry sets `user_books.deleted_at` (and `user_custom_books.deleted_at` for an unverified entry); nothing is hard-deleted except by the daily trash purge (`app/services/trash.py`) after `TRASH_RETENTION_DAYS` (30), by the per-user trash cap (`TRASH_MAX_ENTRIES_PER_USER`, 100: `DELETE` evicts the oldest beyond it) and by account deletion.
- Lock order for library writes: the user's row (`lock_user`) → `user_books` → `user_custom_books`. `DELETE`, restore, `POST /purchased` and `POST /wishlist` take the user lock first; row reads before a write use `with_for_update` and keep the `live()` filter. A response is read back before the commit (`_reread_and_commit`), never after.
- **Every query on `UserBook` or `UserCustomBook` must exclude trashed rows** with `UserBook.live()` / `UserCustomBook.live()` (`deleted_at IS NULL`), unless it is the trash list, restore or the purge. Library reads go through `_user_book_query` in `app/api/user_books.py`, which applies it (`trashed=True` for the trash). This includes counts, caps, duplicate checks (`held_entries`, `UnverifiedBookService`), lookups before a write (`POST /purchased`) and any future stats or export path. `tests/unit/test_soft_delete_trash.py` checks the existing queries; a new query needs its own test that a trashed entry is invisible.
- Uniqueness of `(user_id, book_id)` holds among live rows only (`uq_user_books_user_book_live`, partial). A restore that would make a second live entry of a work is `409 restore_conflict`.
- Account deletion (#291) must delete trashed rows at once, not leave them to the purge.

### Database
- Every new table's migration enables row level security (`op.execute('ALTER TABLE "<name>" ENABLE ROW LEVEL SECURITY')`), with no policies. The app connects as the table owner and bypasses it; it only locks out Supabase's `anon`/`authenticated` roles. `tests/integration/test_rls_enabled_db.py` fails otherwise (#677)
- Never `GRANT` anything to `anon` or `authenticated`. Migration 0023 revoked their table and sequence privileges, including the migrating role's default privileges, so a new table needs no extra step (#704)

## Commands

```bash
# Tests
pytest tests/ --cov=app --cov-fail-under=80

# Lint + format
ruff check .
black --check .

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

The guard's own files (`.github/workflows/migration-guard.yml`, `scripts/migration_guard/`) are listed in `.github/CODEOWNERS`, so changing them needs the owner's review. The `migration-guard` check is listed in the required status checks of `.github/rulesets/protect-main-dev.json`; the owner must apply that ruleset in GitHub settings, otherwise a failing guard does not block the merge. The workflow also triggers on PRs into `main` and passes at once there ("Release PR: migrations were already scanned on their feature PRs into dev"), so the required check reports on dev to main release PRs.

**Label process.** A PR that trips the guard needs the `destructive-migration-approved` label, which only the owner applies; contributors and agents must not add it. Until launch (the app is not live, decision 0) a destructive change may be a single migration with the label instead of a two-release expand/contract; after launch, use expand/contract. Run the scanner locally with `python3 scripts/migration_guard/check_destructive_migrations.py backend/alembic/versions/<file>.py`.

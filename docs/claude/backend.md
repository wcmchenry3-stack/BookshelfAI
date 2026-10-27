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
| `POST /auth/google`, `POST /auth/refresh` | 5/min | `rate_limit_auth` |
| `POST /scan` | 10/min | `rate_limit_scan` |
| `GET /books/search` | 30/min | `rate_limit_books_search` |
| `POST /wishlist`, `POST /wishlist/unverified`, `POST /purchased`, `PATCH`, `DELETE /user-books/*` | 60/min | `rate_limit_writes` |
| `GET /user-books`, `GET /auth/me` | 120/min | `rate_limit_reads` |

All limits are env-var overridable via `Settings` in `app/core/config.py`.

### Auth
- JWT RS256 only — reject all other algorithms including `none`
- Allowlist check via `ALLOWED_EMAILS` env var after Google auth

### Database
- Every new table's migration enables row level security (`op.execute('ALTER TABLE "<name>" ENABLE ROW LEVEL SECURITY')`), with no policies. The app connects as the table owner and bypasses it; it only locks out Supabase's `anon`/`authenticated` roles. `tests/integration/test_rls_enabled_db.py` fails otherwise (#677)

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

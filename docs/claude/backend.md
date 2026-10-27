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
- Users are matched by Google `sub` (`users.google_id`) via `app/auth/identity.py:resolve_google_user`, never by email; an email owned by another subject (compared with `lower()`) → 409 `account_conflict`; `ALLOWED_EMAILS` also ignores case
- `/auth/test-login` exists only in development/test; hardened settings with `TEST_AUTH_SECRET` set fail boot

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

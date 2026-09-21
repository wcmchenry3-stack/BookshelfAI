# Bookshelf (BookShelfAI)

Personal mobile + web app for scanning book covers with your camera, identifying them via AI vision, and tracking your reading collection through **wishlisted → purchased → reading → read**.

Point your camera at a book, the app identifies it (OpenAI `gpt-4o-mini` vision + Google Books enrichment), you pick the right candidate, and it lands in your wishlist. One codebase ships to iOS, Android, and web.

## Stack

**Frontend** — Expo SDK 55 · React Native 0.83 · Expo Router · TypeScript · i18next (10 locales) · Sentry
**Backend** — FastAPI · SQLAlchemy (async) · Alembic · PostgreSQL · slowapi rate limiting · JWT (RS256) auth via Google OAuth
**External** — OpenAI Vision · Google Books API · Open Library · Cloudflare (Turnstile + WAF) · Sentry
**Infra** — Render (backend + Postgres) · Xcode Cloud (iOS) · local Gradle → Play Console (Android) · GitHub Actions CI

## Repo map

```
book_app/
├── backend/         FastAPI app, SQLAlchemy models, Alembic migrations, pytest suite
├── frontend/        Expo (iOS + Android + web) app, Jest tests, native Xcode + Gradle projects
├── docs/            All project documentation (this README is the hub)
│   └── claude/      Claude-specific instructions for working inside this repo
├── .github/         CI workflows, PR template, rulesets
├── CLAUDE.md        Load-bearing rules for Claude Code sessions (read before contributing)
├── render.yaml      Render service + DB definition (dev branch auto-deploys the -dev services; prod deploys from main only via .github/workflows/deploy.yml after CI)
└── .tool-versions   asdf/mise version pins (JDK 17 for Android)
```

## Quickstart

Full end-to-end setup is in [`docs/getting-started.md`](docs/getting-started.md). Short version:

```bash
# 1. Backend
cd backend && python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env      # fill in Google OAuth, Google Books, OpenAI, Sentry keys
alembic upgrade head
uvicorn app.main:app --reload

# 2. Frontend (new terminal)
cd frontend && npm install
npx expo start            # press i for iOS sim, a for Android, w for web
```

**Prerequisites:** PostgreSQL 15+, Python 3.12+, Node 20.x, JDK 17 (`export JAVA_HOME=$(/usr/libexec/java_home -v 17)`) for Android, Xcode for iOS.

## Workflow rules (summary)

- Branch from `dev` → `feature/<name>` or `bug/<name>` → open PR targeting `dev` → CI green → merge
- **Never** push directly to `dev` or `main`
- `dev` auto-deploys `bookshelf-api-dev`/`bookshelf-web-dev`; production (`bookshelf-api`/`bookshelf-web`, branch `main`) only deploys via `.github/workflows/deploy.yml` after CI passes — see [`docs/RENDER.md`](docs/RENDER.md)
- Full rule list: [`CLAUDE.md`](CLAUDE.md)

## Documentation

| Doc | Purpose |
|---|---|
| [`docs/getting-started.md`](docs/getting-started.md) | End-to-end local dev setup (DB + backend + frontend + first scan) |
| [`docs/architecture.md`](docs/architecture.md) | System design, middleware stack, auth architecture, decision records |
| [`docs/project-structure.md`](docs/project-structure.md) | Monorepo layout, conventions, "how to add a ..." recipes |
| [`docs/features.md`](docs/features.md) | User-facing features, scan flow + auth flow sequence diagrams |
| [`docs/data-model.md`](docs/data-model.md) | ER diagram, entity reference, book-dedup strategy |
| [`docs/api.md`](docs/api.md) | REST endpoint reference with examples and error codes |
| [`docs/integrations.md`](docs/integrations.md) | Google OAuth / Books / OpenAI / Open Library / Sentry / Cloudflare / Render |
| [`docs/deployment.md`](docs/deployment.md) | Render config, env vars, release process, rollback |
| [`docs/testing.md`](docs/testing.md) | Test strategy (unit/integration/security/accuracy/perf), patterns, how to run |
| [`docs/security.md`](docs/security.md) | Security controls, OWASP test cases, incident runbook |
| [`docs/claude/`](docs/claude/) | Claude-specific guides (git workflow, iOS/Android CI, backend/frontend conventions) |

## For Claude sessions

Start by reading [`CLAUDE.md`](CLAUDE.md). It covers branching rules, testing requirements, native build constraints (iOS and Android), and the hard rules this repo enforces (no raw SQL, no hardcoded colors, no `AsyncStorage` for tokens, every endpoint rate-limited, i18n on every user-facing string, etc.).

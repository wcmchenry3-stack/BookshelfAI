# Project structure

Where everything lives + the naming conventions + "how to add a ..." recipes. For architecture see [`architecture.md`](architecture.md); for the features that live in each folder see [`features.md`](features.md).

## Monorepo layout

```
book_app/
├── backend/               FastAPI + SQLAlchemy + Alembic (Python 3.12)
├── frontend/              Expo SDK 55 (React Native + Web, iOS + Android)
├── docs/                  All project documentation (this tree)
├── .github/               CI workflows, PR template, rulesets, CODEOWNERS
├── CLAUDE.md              Hard rules + detail-doc links for Claude sessions
├── README.md              Top-level hub + doc index
├── SECURITY.md            Security contact + policy
├── render.yaml            Render service definition (backend + Postgres)
└── .tool-versions         asdf/mise pins (Java zulu-17.64.17)
```

## Backend — `backend/`

```
backend/
├── alembic/
│   ├── env.py             Alembic runtime config
│   └── versions/          Migration files (rule #7: ORM only — no raw SQL)
├── alembic.ini            Alembic config
├── app/                   Application package
│   ├── main.py            FastAPI app, middleware stack, /health endpoint
│   ├── ai/                AI gateway — the only place paid model calls happen
│   │   ├── entitlements.py    tier_for(): server-side Free/Premium tier (Free for everyone for now)
│   │   ├── gateway.py         run_feature(): single entry point for AI features
│   │   ├── models_allowlist.py Allowed models and token/timeout bounds (shared by config and registry)
│   │   ├── registry.py        Server-controlled FeatureSpec registry (validated at import)
│   │   └── providers/         Provider adapters (only package allowed to call a provider)
│   │       ├── _common.py         Reply parsing shared by the adapters (ISBNs, confidence, logging)
│   │       ├── base.py            VisionProvider protocol
│   │       └── openai_vision.py   OpenAI vision book identifier
│   ├── api/               HTTP routers (endpoints)
│   │   ├── auth.py
│   │   ├── books.py
│   │   ├── scan.py
│   │   └── user_books.py
│   ├── auth/              Auth primitives
│   │   ├── dependencies.py    get_current_user FastAPI dep
│   │   ├── google.py          verify_google_id_token + JWKS fetch
│   │   └── jwt.py             RS256 encode/decode + token factories
│   ├── core/              Cross-cutting infrastructure
│   │   ├── config.py          Pydantic Settings (env-var surface)
│   │   ├── database.py        AsyncEngine, session factory, Base
│   │   ├── file_validation.py Magic-byte validation for uploads
│   │   ├── limiter.py         slowapi Limiter instance
│   │   ├── logging.py         stdlib logging + request-id contextvar
│   │   └── sentry_context.py  SentryContextMiddleware
│   ├── models/            SQLAlchemy ORM models (one file per table; shared columns in _mixins.py)
│   │   ├── book.py
│   │   ├── edition.py
│   │   ├── refresh_token.py
│   │   ├── user_book.py
│   │   └── user.py
│   ├── schemas/           Pydantic request/response models
│   │   ├── auth.py
│   │   ├── book.py
│   │   └── user_book.py
│   └── services/          Domain logic (stateless, reusable)
│       ├── book_identifier.py     Abstract interface + BookCandidate dataclass
│       ├── deduplication.py       Per-user library dedup check
│       ├── enrichment/            Enrich candidates via Google Books + Open Library
│       │   ├── service.py         EnrichmentService (provider chains, ISBN lookup)
│       │   ├── scoring.py         Reading-vs-record fit, provider agreement
│       │   ├── titles.py          Title / subtitle comparison
│       │   ├── authors.py         Author-name comparison
│       │   ├── isbn.py            ISBN validation and conversion
│       │   └── text.py            Text normalisation
│       ├── _lookup_http.py        GET + JSON helper shared by the two lookup clients
│       ├── google_books.py        Google Books API client
│       ├── open_library.py        Open Library API client
│       └── wishlist.py            POST /wishlist business logic
├── tests/                 pytest suite
│   ├── conftest.py            Shared fixtures (async DB session, app client)
│   ├── unit/                  Per-module unit tests
│   ├── integration/           Cross-module / DB integration
│   ├── security/              OWASP-aligned (pytest -m security)
│   ├── accuracy/              Dedup + enrichment accuracy benchmarks
│   └── performance/           pytest-benchmark suites
├── requirements.txt       Runtime deps (pinned)
├── requirements-dev.txt   Dev/test deps (includes requirements.txt)
├── pytest.ini             pytest config (markers, async mode)
└── Dockerfile             Container build (used by Render)
```

**Layer contract:**
- `api/` routers are thin — auth / rate limit decorator + request/response mapping → call into `services/`
- `services/` hold the actual logic — they take a DB session + typed args, return domain objects. No HTTP concepts leak in.
- `models/` = SQLAlchemy tables. `schemas/` = Pydantic request/response. **Never mix them** — schemas have `model_config = {"from_attributes": True}` to marshal from ORM objects.
- `core/` is for cross-cutting stuff that every layer uses (config, DB, logging, middleware primitives).
- `auth/` is kept separate from `core/` because it's the one piece of infrastructure with its own test surface and external dependency (Google JWKS).

## Frontend — `frontend/`

```
frontend/
├── app/                   expo-router file-based routes
│   ├── _layout.tsx            Root layout: Sentry init (MUST be first import,
│   │                          rule #13), providers, error boundary
│   ├── index.tsx              Bootstrap route → redirects to (auth) or (tabs)
│   ├── (auth)/                Unauthenticated stack
│   │   └── login.tsx          Google Sign-In
│   └── (tabs)/                Authenticated tab bar
│       ├── _layout.tsx        Tab bar config
│       ├── scan.tsx           Camera + scan entry
│       ├── my-books.tsx       Library + filter + detail modal
│       ├── wishlist.tsx       Wishlist filter view
│       └── settings.tsx       Language, theme, sign-out
├── components/            Reusable UI (rule #5: useTheme(), never hardcoded colors)
│   ├── BookCandidatePicker.tsx    3-card chooser for scan results
│   ├── ErrorBoundary.tsx          Top-level error catcher
│   ├── InAppBanner.tsx            Toast/banner host
│   ├── LoadingSpinner.tsx
│   └── ThemeToggleButton.tsx
├── contexts/              React Context providers
│   ├── AuthContext.tsx            Token storage, session hydration + launch /auth/me check, login/logout
│   ├── BannerContext.tsx          InAppBanner imperative API
│   └── ScanJobContext.tsx         Scan queue state machine + offline drain
├── hooks/                 Thin hooks over contexts
│   ├── useAuth.ts
│   ├── useBanner.ts
│   ├── useNetworkStatus.ts
│   ├── useScanJobs.ts
│   └── useTheme.ts
├── lib/                   Non-React modules
│   ├── api.ts                     Axios instance + auth header interceptor
│   ├── freshInstall.ts            Clears Keychain tokens left by a previous install (#722)
│   ├── scanJob.ts                 ScanJob types + state enum
│   ├── scanJobStorage.ts          expo-file-system persistence for scan queue
│   ├── sentry.ts                  Sentry init + captureException wrapper
│   └── storage.ts                 expo-secure-store for tokens (rule #6)
├── src/i18n/              i18next setup + all translations
│   ├── i18n.ts                    Static en import + lazy importMap for all locales
│   ├── locales.ts                 LOCALES[] registry
│   ├── glossary.ts                Translation consistency glossary
│   └── locales/
│       ├── _meta/                 Key descriptions (per namespace)
│       ├── en/                    Source of truth (bundled)
│       ├── fr-CA/, es/, hi/, zh/, ja/, ko/, pt/, de/, nl/, ru/   Active locales
│       └── ar/, he/               Present, excluded until RTL lands
├── theme/                 Design tokens + ThemeContext
│   ├── tokens.ts                  Spacing, typography, radii
│   ├── light.ts / dark.ts         Color palettes
│   └── ThemeContext.tsx
├── __tests__/             Jest suite
│   ├── unit/
│   └── integration/
├── android/               Native Android project (bare Expo prebuild)
├── ios/                   Native iOS project (bare Expo prebuild)
├── scripts/
│   └── translate.js               i18n workflow helper
├── app.json               Expo config
├── babel.config.js
├── jest.setup.ts
├── package.json           engines: node 20.x, npm <11 (rule #12)
├── tsconfig.json
└── .eslintrc.js           no-restricted-imports rule bans `uuid` (#183 regression guard)
```

**Layer contract:**
- `app/` is **routing + screen composition only** — screens call hooks and render components. No domain logic.
- `contexts/` own state + side-effects (API calls, persistence, NetInfo listeners).
- `hooks/` are thin wrappers exposing context values. One hook per context usually.
- `components/` are presentational — they never call contexts directly, they take props.
- `lib/` is framework-agnostic. Things in `lib/` shouldn't import from `components/` or `contexts/`.
- `theme/` is the only place colors live (rule #5).

## Naming conventions

**Backend (Python):**
- Modules: `snake_case.py`
- Classes: `PascalCase`
- Functions, variables: `snake_case`
- Constants: `UPPER_SNAKE`
- SQLAlchemy tables: `__tablename__ = "plural_snake"` (users, user_books, refresh_tokens)
- FK columns: `<table>_id` (singular + `_id`)
- Timestamps: `*_at` (`created_at`, `purchased_at`)
- Pydantic schemas: `<Entity><Purpose>` — `UserRead`, `WishlistRequest`, `UserBookUpdate`

**Frontend (TypeScript):**
- Components: `PascalCase.tsx`
- Hooks: `useXxx.ts`
- Contexts: `<Name>Context.tsx` (exports both the context and the `<Name>Provider`)
- Tests: `<SubjectUnderTest>.test.tsx` mirroring the source tree inside `__tests__/`
- i18n namespaces: lowercase kebab (`my-books`, `components`)

## How to add...

### ...a new API endpoint
1. Define request/response Pydantic models in `backend/app/schemas/<domain>.py`
2. Add any new SQLAlchemy model in `backend/app/models/<entity>.py`
3. If the schema changed, autogenerate a migration: `alembic revision --autogenerate -m "..."` (see [data-model.md](data-model.md#migration-workflow) for the CheckConstraint gotcha)
4. Write the handler in `backend/app/api/<domain>.py`:
   ```python
   @router.<verb>("/path", response_model=<ResponseSchema>)
   @limiter.limit(settings.rate_limit_<bucket>)  # rule #8 — REQUIRED
   async def handler(
       request: Request,
       body: <RequestSchema>,
       current_user: User = Depends(get_current_user),
       db: AsyncSession = Depends(get_db),
   ) -> ...:
       ...
   ```
5. If the logic is non-trivial, factor it into `backend/app/services/<domain>.py`
6. Add unit tests in `backend/tests/unit/test_<domain>.py` (mock the DB via `conftest.py` fixtures)
7. Add the endpoint to [`api.md`](api.md) in the same PR (the doc-reminder workflow will flag this)

### ...a new screen
1. Decide: does it need auth? → file under `app/(tabs)/` or `app/(auth)/`
2. Create the file — filename becomes the route: `app/(tabs)/reading-stats.tsx` → `/reading-stats` (inside the tab bar)
3. Register in the tab layout if needed: `app/(tabs)/_layout.tsx`
4. Pull data via a hook that wraps a context (don't call `api.ts` directly from the screen)
5. i18n every user-facing string (rule #9) — add strings to the appropriate namespace in `src/i18n/locales/en/<namespace>.json`
6. Theme every color via `useTheme()` (rule #5)
7. Test in `__tests__/unit/<screen-name>.test.tsx`
8. Update [`features.md`](features.md) with the new screen

### ...a new DB migration
See [data-model.md → Migration workflow](data-model.md#migration-workflow). Short version:
```bash
cd backend && source .venv/bin/activate
alembic revision --autogenerate -m "add <thing>"
# Review the generated file (autogen misses CheckConstraints)
alembic upgrade head
```

### ...a new i18n string
1. Add the key to `frontend/src/i18n/locales/en/<namespace>.json`
2. Add a description + context to `frontend/src/i18n/locales/_meta/<namespace>.meta.json`
3. Translate into all 10 active non-en locales (or mark `// TODO translate` and ship English fallback)
4. Use in component:
   ```tsx
   const { t } = useTranslation('<namespace>');
   return <Text>{t('yourKey')}</Text>;
   ```
5. The translation helper at `frontend/scripts/translate.js` can batch-translate if configured

### ...a new feature branch + PR
From rules #1–#3:
```bash
git checkout dev && git pull origin dev
git checkout -b feature/<name>    # or bug/<name>
# ... make changes, commit ...
git push -u origin feature/<name>
gh pr create --draft --base dev --title "..."
```
Never commit to `dev` or `main` directly. Never push to `main`.

## Rules touched by this layout

- Rule #5: Use `useTheme()` — enforced by conventions here + theme/ location
- Rule #6: `SecureStore` for tokens — in `lib/storage.ts`, never `AsyncStorage`
- Rule #7: SQLAlchemy ORM only — `models/` + `services/` (no `text("SELECT ...")` for business queries)
- Rule #8: Every endpoint has `@limiter.limit(...)` — checked in the "how to add an endpoint" recipe
- Rule #9: i18n on every user-facing string — `src/i18n/locales/`
- Rule #12: Never change Node version pins — enforced via `package.json` engines
- Rule #13: Sentry first import in `app/_layout.tsx` — explicit in the frontend layout

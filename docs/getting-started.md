# Getting started

The doc you hit every time you re-clone. End-to-end local setup: Postgres, backend, frontend, env keys, first scan.

For what the app does and the architectural shape, see [`architecture.md`](architecture.md). For the REST surface, see [`api.md`](api.md).

## Prerequisites

| Tool | Version | Why | How to install |
|---|---|---|---|
| PostgreSQL | 15+ | Primary DB (matches Render prod) | `brew install postgresql@15` |
| Python | 3.12+ | Backend (Render uses 3.12) | `brew install python@3.12` |
| Node | 20.19.4 ≤ v < 23 | Frontend (pinned in `frontend/package.json` engines) | `brew install node@20` or asdf/mise |
| npm | ≥10 <11 | Strictly required — npm 11 has known crashes (rule #12) | ships with Node 20 |
| JDK 17 | exactly | Android builds fail on JDK 24+ (rule #22) | `brew install --cask zulu@17` |
| Xcode | latest | iOS builds (Xcode Cloud for CI, local for dev) | Mac App Store |
| CocoaPods | current | iOS native deps (`pod install`, rule #10) | `sudo gem install cocoapods` |

**Version manager alternative:** `.tool-versions` at repo root pins `java zulu-17.64.17` — `asdf install` / `mise install` picks it up.

**JDK 17 for Android sessions:**
```bash
export JAVA_HOME=$(/usr/libexec/java_home -v 17)
```
The Gradle preflight in `frontend/android/settings.gradle` fails fast with a clear error if the wrong JDK is on the path.

## 1. Clone and install

```bash
git clone https://github.com/wcmchenry3-stack/book_app.git
cd book_app
```

**Backend:**
```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

**Frontend:**
```bash
cd ../frontend
npm install
```

## 2. Postgres bootstrap

```bash
# Start Postgres if not already running
brew services start postgresql@15

# Create the DB
createdb bookshelf

# Run migrations
cd ../backend
source .venv/bin/activate
alembic upgrade head
```

Verify the schema is in place:
```bash
psql bookshelf -c "\dt"
```
You should see `users`, `books`, `editions`, `user_books`, `refresh_tokens`, `alembic_version`.

## 3. Backend `.env`

```bash
cd backend
cp .env.example .env
```

Fill in each required variable. Skip anything marked *optional*.

### Database
```bash
DATABASE_URL=postgresql+asyncpg://<your-mac-username>@localhost:5432/bookshelf
```
If you set a password on your local Postgres, include it: `postgresql+asyncpg://user:password@localhost:5432/bookshelf`.

### Auth — Google OAuth

You need three Google OAuth client IDs (one per platform) from the same Google Cloud project. Create them at **Google Cloud Console → APIs & Services → Credentials → Create Credentials → OAuth client ID**:

| Var | Application type | Notes |
|---|---|---|
| `GOOGLE_CLIENT_ID` | Web application | Add `http://localhost:8081` to "Authorized JavaScript origins" |
| `GOOGLE_IOS_CLIENT_ID` | iOS | Bundle ID: `com.buffingchi.bookshelf` |
| `GOOGLE_ANDROID_CLIENT_ID` | Android | Package name: `com.buffingchi.bookshelf` + SHA-1 from `debug.keystore` |
| `GOOGLE_CLIENT_SECRET` | (Web client secret) | From the Web client's credential page |

The backend's `verify_google_id_token()` accepts tokens whose `aud` claim matches any of these three IDs, so the same API serves web, iOS, and Android.

### Auth — JWT RS256 keys

Generate a fresh RSA keypair locally (never commit these):
```bash
openssl genpkey -algorithm RSA -out jwt-private.pem -pkeyopt rsa_keygen_bits:2048
openssl rsa -pubout -in jwt-private.pem -out jwt-public.pem
```

The `.env` values must be single-line with `\n` escapes. Quick conversion:
```bash
awk 'NF {sub(/\r/, ""); printf "%s\\n", $0}' jwt-private.pem
awk 'NF {sub(/\r/, ""); printf "%s\\n", $0}' jwt-public.pem
```
Paste each output into `JWT_PRIVATE_KEY` / `JWT_PUBLIC_KEY` (wrapped in double quotes).

### Auth — allowlist
```bash
ALLOWED_EMAILS=you@example.com
```
Comma-separated list of emails permitted to authenticate. Single-user lockdown by default.

### External APIs
```bash
OPENAI_API_KEY=sk-...                    # https://platform.openai.com/api-keys
GOOGLE_BOOKS_API_KEY=AIza...             # Google Cloud → Books API → credentials
OPEN_LIBRARY_BASE_URL=https://openlibrary.org  # leave as-is
```

### Optional
```bash
SENTRY_DSN=                              # leave blank to disable
TURNSTILE_SECRET_KEY=                    # leave blank to skip Turnstile check on /scan
```

### App
```bash
ENVIRONMENT=development
CORS_ORIGINS=http://localhost:8081,exp://localhost:8081
```
In dev, `CORS_ORIGINS` must include the Expo dev-server origin. The wildcard `*` is blocked by a validator in `app/core/config.py`.

## 4. Frontend `.env`

```bash
cd ../frontend
cp .env.example .env
```

Set:
```bash
EXPO_PUBLIC_API_URL=http://localhost:8000             # points at local backend
EXPO_PUBLIC_GOOGLE_WEB_CLIENT_ID=<same as backend's GOOGLE_CLIENT_ID>
EXPO_PUBLIC_GOOGLE_IOS_CLIENT_ID=<same as backend's GOOGLE_IOS_CLIENT_ID>
EXPO_PUBLIC_GOOGLE_ANDROID_CLIENT_ID=<same as backend's GOOGLE_ANDROID_CLIENT_ID>
EXPO_PUBLIC_SENTRY_DSN=                               # optional
EXPO_PUBLIC_ENVIRONMENT=development
```

Only `EXPO_PUBLIC_*` vars are exposed to the client bundle — anything else is server-only.

## 5. Run backend

```bash
cd backend
source .venv/bin/activate
uvicorn app.main:app --reload
```

Verify it's up:
```bash
curl http://localhost:8000/health
# {"status":"ok"}
curl http://localhost:8000/health/db
# {"status":"ok","db":"ok"}
```

Interactive docs (non-prod only): http://localhost:8000/docs

## 6. Run frontend

New terminal:
```bash
cd frontend
npx expo start
```

Press:
- `w` — opens web in your browser (fastest iteration)
- `i` — opens iOS simulator (Xcode required)
- `a` — opens Android emulator (must be booted; see below)

**Android emulator** — from a different terminal so the ongoing Metro server isn't blocked:
```bash
emulator -avd <your-avd-name>    # boot emulator
export JAVA_HOME=$(/usr/libexec/java_home -v 17)
npx expo run:android             # native build + install
```
First-time Android build also needs a debug keystore — see [android-ci.md](claude/android-ci.md) → "First-time local Android setup".

## 7. First end-to-end test

1. **Sign in with Google** — on the login screen, use an email in your `ALLOWED_EMAILS` list.
2. **Take a photo of a book cover** (scan tab). On web/simulator you can upload from filesystem.
3. **Watch the backend log** — you should see a `POST /scan` then breadcrumbs for the image-identify → enrichment → dedup pipeline.
4. **Confirm a candidate** — it lands in your wishlist.
5. **Check `my-books` tab** — the book appears with `wishlisted` status.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `psql: FATAL: database "bookshelf" does not exist` | skipped `createdb bookshelf` | Run step 2 |
| `alembic.util.exc.CommandError: Can't locate revision` | wrong alembic version | `alembic upgrade head` from `backend/` with venv active |
| Backend starts but `/health/db` returns `db: error` | `DATABASE_URL` wrong or Postgres down | Check `brew services list`, verify username + DB name |
| Sign-in fails with 401 | email not in `ALLOWED_EMAILS` | add to `.env`, restart uvicorn |
| Sign-in fails with `Invalid audience` | frontend client ID doesn't match backend's list | ensure all three `GOOGLE_*_CLIENT_ID` vars match between `.env` files |
| `/scan` returns 500 | `OPENAI_API_KEY` missing or invalid | Check OpenAI dashboard; key needs Vision access |
| Scanned book has no cover | Google Books / Open Library rate-limited or offline | Retry; both APIs have rolling limits |
| iOS sim black screen | Metro not bundling | kill `npx expo start`, restart, press `i` again |
| Android build fails at `:app:configureCMakeDebug` | wrong JDK | `export JAVA_HOME=$(/usr/libexec/java_home -v 17)` |
| Android build fails at `:app:validateSigningDebug` | missing debug keystore | run the `keytool` command from [`claude/android-ci.md`](claude/android-ci.md) |
| `crypto.getRandomValues() not supported` in scan flow | `uuid` reintroduced somewhere | lint rule in `frontend/.eslintrc.js` blocks this — check PR diff |

## Useful commands

```bash
# Backend
cd backend && source .venv/bin/activate
uvicorn app.main:app --reload            # dev server
pytest tests/ --cov=app                  # tests + coverage
pytest -m security                       # security suite only
alembic revision --autogenerate -m "..."  # new migration
alembic upgrade head                     # apply migrations
alembic downgrade -1                     # roll back one

# Frontend
cd frontend
npx expo start                           # dev server
npx expo start --web                     # web only
npx expo run:ios                         # native iOS build
npx expo run:android                     # native Android build
npx jest                                 # all tests
npx jest __tests__/unit/foo.test.tsx     # single test
npx eslint .                             # lint
npx prettier --write .                   # format
```

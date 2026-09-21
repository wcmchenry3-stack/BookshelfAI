# Deployment

How the app gets shipped. Four separate pipelines — one per platform. Everything is triggered by git pushes or manual store uploads; no EAS (rules #1–3 in the native-build section of `CLAUDE.md`).

For iOS + Android build infrastructure deep-dives, see [`claude/ios-ci.md`](claude/ios-ci.md) and [`claude/android-ci.md`](claude/android-ci.md). For the architectural constraints driving the deploy layout, see [`architecture.md`](architecture.md). For the Render dev/prod topology and Supabase connection rules, see [`RENDER.md`](RENDER.md).

## Pipelines at a glance

| Surface | Target | Trigger | Config file |
|---|---|---|---|
| Backend API | Render (`bookshelf-api` web service) | git push (branch per Render dashboard) | `render.yaml` |
| Web frontend | Render (`bookshelf-web` static site) | git push to `dev` | `render.yaml` |
| iOS app | Xcode Cloud → App Store Connect | git push to `main` (Xcode Cloud workflow) | `frontend/ios/ci_scripts/` |
| Android app | Local `./gradlew bundleRelease` → Play Console | manual | `frontend/android/app/build.gradle` |

## Backend — Render

**Service name:** `bookshelf-api`
**Region:** Oregon
**Plan:** starter
**Source:** `render.yaml`

**Build:** `pip install -r requirements.txt`
**Start:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
**Pre-deploy:** `alembic upgrade head` (migrations run before the new version takes traffic — a failing migration blocks the deploy)
**Health check:** `/health` (liveness only, never touches the DB — a Render restart must not be triggered by a transient DB blip; use `/health/db` to check DB connectivity)

**Branch:** set in the Render dashboard (not in `render.yaml`). Check with `render services get bookshelf-api` or by looking at the service settings in the dashboard.

### Env vars baked into `render.yaml`

```
ENVIRONMENT=production
CORS_ORIGINS=["https://bookshelfai.buffingchi.com"]
TRUSTED_HOSTS=["bookshelfapi.buffingchi.com"]
DATABASE_URL              (auto, from bookshelf-db)
TURNSTILE_SECRET_KEY      (sync:false — set in dashboard)
SENTRY_DSN                (sync:false — set in dashboard)
```

**`ENVIRONMENT` values:** `development` | `test` | `staging` | `production`. Only `development` and `test` are treated as trusted/local (`settings.is_hardened == False`). Any other value — including `staging` — gets full production-grade hardening: `/docs` disabled, `TrustedHostMiddleware` enforced, HSTS sent, `CF-Connecting-IP` trusted, and the `/debug/sentry-test` + `/auth/test-login` routes not even registered. `staging` still reports as `staging` (not `production`) to Sentry so events are distinguishable. A public staging deploy must always use `ENVIRONMENT=staging`, never `development`.

### Env vars that live only in the Render dashboard

These aren't in `render.yaml` because they're secrets or because they change independently. Set them in the Render dashboard for the `bookshelf-api` service:

- `GOOGLE_CLIENT_ID`, `GOOGLE_IOS_CLIENT_ID`, `GOOGLE_ANDROID_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`
- `JWT_PRIVATE_KEY`, `JWT_PUBLIC_KEY`
- `ALLOWED_EMAILS`
- `OPENAI_API_KEY`
- `GOOGLE_BOOKS_API_KEY`
- `OPENAI_MAX_TOKENS` (optional)

### Database

`bookshelf-db` — Postgres, `basic-256mb` plan, Oregon. Connection string is auto-injected into the API service.

### Release process (backend)

1. Merge your PR into `dev` (via CI-green draft PR per rules #1-#3).
2. Render picks up the push and starts a new deploy.
3. `alembic upgrade head` runs; if it fails the deploy aborts and traffic stays on the old version.
4. New process boots, `/health` passes, Render flips traffic over.
5. Watch Sentry for new errors + Render logs for anything unexpected.

Verify the new version is live:
```bash
curl https://bookshelfapi.buffingchi.com/health
# {"status":"ok"}
curl https://bookshelfapi.buffingchi.com/health/db
# {"status":"ok","db":"ok"}
```

### Rollback (backend)

Two options:
- **Render dashboard → Deploys → "Rollback to this deploy"** — flips traffic back to a prior successful deploy. Fast (seconds).
- **Git revert + push** — creates a new commit that undoes the change. Correct when the bad deploy succeeded but the code is bad. Slower (waits for the next deploy cycle).

If a migration is the problem, the rollback deploy will re-run `alembic upgrade head` which is a no-op against the current schema. You may need an explicit `alembic downgrade -1` migration to reverse the schema change.

## Web frontend — Render

**Service name:** `bookshelf-web`
**Type:** static site
**Branch:** `dev` (explicit in `render.yaml`)
**Region:** Oregon

**Build:** `npm ci` → write `.env` from Render-injected vars → `npx expo export --platform web --clear`
**Publish:** `dist/` directory
**Routing:** SPA rewrite (`/*` → `/index.html`)

### Security headers (set via `render.yaml`)

Every response from the static site carries:
- `Cross-Origin-Opener-Policy: same-origin-allow-popups`
- `X-Frame-Options: DENY`
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Strict-Transport-Security: max-age=31536000; includeSubDomains`
- `Content-Security-Policy: default-src 'self'; img-src 'self' https: data:; connect-src 'self' https://bookshelfapi-dev.buffingchi.com; style-src 'self' 'unsafe-inline'; script-src 'self'`

**Caching:**
- `/*` → `no-cache` (HTML doesn't cache)
- `/assets/*` → `public, max-age=31536000, immutable` (hashed bundle files)

### Env vars (web)

`render.yaml` defines:
- `EXPO_PUBLIC_API_URL=https://bookshelfapi-dev.buffingchi.com`
- `EXPO_PUBLIC_ENVIRONMENT=development`

Dashboard-only (sync:false):
- `EXPO_PUBLIC_GOOGLE_WEB_CLIENT_ID`
- `EXPO_PUBLIC_SENTRY_DSN`

### Release process (web)

Any push to `dev` triggers a static rebuild + redeploy. No separate promotion step — `dev` is what the web surface runs.

## iOS app — Xcode Cloud

**Build system:** Xcode Cloud (not EAS)
**Submission:** direct to App Store Connect

**Workflow scripts:** `frontend/ios/ci_scripts/ci_post_clone.sh`, `frontend/ios/ci_scripts/ci_post_xcodebuild.sh`.

For the full iOS CI+submission pipeline (post-clone prep, pod install, bundle validation, Node version pins, Sentry upload), see [`claude/ios-ci.md`](claude/ios-ci.md) and [`claude/ios-appstore-submission.md`](claude/ios-appstore-submission.md).

### Release process (iOS)

Summary — details in the Claude docs:
1. Bump `CFBundleShortVersionString` + `CFBundleVersion` in Xcode (or via `frontend/app.json`).
2. Push to the branch Xcode Cloud is watching.
3. Xcode Cloud builds + archives + uploads to App Store Connect.
4. Promote the build to TestFlight (manual) → submit for review → release.

### Rollback (iOS)

App Store Connect does not support app-version rollback. Options:
- Phase a new build (bumped version) that reverts the change.
- Use Apple's "Reject this version" on an in-review build before it ships.

## Android app — local Gradle + Play Console

**Build system:** `./gradlew bundleRelease` run locally (not EAS, not GitHub Actions)
**Submission:** direct upload to Play Console

### Release process (Android)

Summary — details in [`claude/android-ci.md`](claude/android-ci.md):
1. Bump `versionCode` + `versionName` in `frontend/android/app/build.gradle`.
2. Ensure `JAVA_HOME` points at JDK 17 (rule #22 / preflight check in `settings.gradle`).
3. `cd frontend/android && ./gradlew bundleRelease`
4. Sign the AAB with the release keystore (stored offline, not in repo).
5. Upload the signed AAB to Play Console → Internal Testing → promote to production.

### Rollback (Android)

Play Console supports promoting an older release to production:
1. Play Console → Production → Create new release
2. Use a previously-uploaded AAB (tracked releases)
3. Roll it out (can do a staged rollout — 10% / 50% / 100%)

## Monitoring after deploy

- **Sentry** — watch the project dashboard for new error spikes in the release tag matching the deploy
- **Render logs** — dashboard → `bookshelf-api` → Logs; filter by `level:error` or `level:warning`
- **Render health** — `/health` endpoint, included as a CI job on every PR (see `.github/workflows/ci.yml` → `backend-health`)
- **Cloudflare analytics** — request volume + origin response codes for the `buffingchi.com` zone

## Environment map

| Env | Backend URL | Web URL | Native apps point at |
|---|---|---|---|
| Production | `https://bookshelfapi.buffingchi.com` | (native apps) | `bookshelfapi.buffingchi.com` |
| Dev/staging (web) | `https://bookshelfapi-dev.buffingchi.com` | Render `bookshelf-web` | n/a |
| Local | `http://localhost:8000` | `http://localhost:8081` | `EXPO_PUBLIC_API_URL` override |

See [`getting-started.md`](getting-started.md) for the full local-env setup.

## Common release gotchas

| Symptom | Cause | Fix |
|---|---|---|
| Render deploy fails at `alembic upgrade head` | migration error | fix the migration, re-push; the old version keeps serving traffic |
| `/health/db` returns 503 after deploy | DB unreachable | check `bookshelf-db` status in Render dashboard |
| Web site serving old JS | browser cached `/*` | `Cache-Control: no-cache` is set on HTML but browsers sometimes cache aggressively — hard-reload |
| iOS build fails at `ci_post_clone.sh` | Node version mismatch (rule #12) | see `claude/ios-ci.md` |
| Android `bundleRelease` fails with CMake error | JDK 24+ on path | `export JAVA_HOME=$(/usr/libexec/java_home -v 17)` (rule #22, preflight in `settings.gradle`) |
| App Store / Play Console reject version bump | `versionCode` not incremented | bump `versionCode` (Android) / `CFBundleVersion` (iOS) every submission |

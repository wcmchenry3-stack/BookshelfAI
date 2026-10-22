# Deployment

How the app gets shipped. Four separate pipelines — one per platform. Everything is triggered by git pushes or manual store uploads; no EAS (rules #1–3 in the native-build section of `CLAUDE.md`).

For iOS + Android build infrastructure deep-dives, see [`claude/ios-ci.md`](claude/ios-ci.md) and [`claude/android-ci.md`](claude/android-ci.md). For the architectural constraints driving the deploy layout, see [`architecture.md`](architecture.md). For the Render dev/prod topology and Supabase connection rules, see [`RENDER.md`](RENDER.md). For nightly database backups and the restore drill, see [`backups.md`](backups.md).

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
**Start:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT --no-proxy-headers`
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

**`ENVIRONMENT` values:** `development` | `test` | `staging` | `production`. Only `development` and `test` are treated as trusted/local (`settings.is_hardened == False`). Any other value — including `staging` — gets full production-grade hardening: `/docs` disabled, `TrustedHostMiddleware` enforced, HSTS sent, and the `/debug/sentry-test` + `/auth/test-login` routes not even registered. `staging` still reports as `staging` (not `production`) to Sentry so events are distinguishable. A public staging deploy must always use `ENVIRONMENT=staging`, never `development`.

### Env vars that live only in the Render dashboard

These aren't in `render.yaml` because they're secrets or because they change independently. Set them in the Render dashboard for the `bookshelf-api` service:

- `GOOGLE_CLIENT_ID`, `GOOGLE_IOS_CLIENT_ID`, `GOOGLE_ANDROID_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`
- `JWT_PRIVATE_KEY`, `JWT_PUBLIC_KEY`
- `ALLOWED_EMAILS`
- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY` (#532) — only needed once a scan feature is switched to the `anthropic` provider (see "Switching a scan feature to Claude" below). The app boots without it; a feature switched to `anthropic` while it is missing answers `503 scan_unavailable` (refunded) and logs an error. Secret: never logged, scrubbed from Sentry events
- `GOOGLE_BOOKS_API_KEY`
- `OPENAI_MAX_TOKENS` (optional)

Scan dials (optional; defaults shown; change in the dashboard and restart, no release needed — out-of-range values fail at boot):

- `SCAN_ENABLED` (`true`), `SCAN_ENHANCED_ENABLED` (`true`), `SCAN_MAX_IMAGE_PIXELS`, `SCAN_MODEL`, `SCAN_ENHANCED_MODEL` — **initial defaults only** (#526). They seed the `ai_settings` table on first boot (see "AI settings" below); after that, changing the env var does nothing for a key that already has a row. They are also the fallback for a key whose row is missing
- `SCAN_GLOBAL_DAILY_LIMIT` (`2000`, range 1–1,000,000) — **initial default only** (#528): seeds `scan.global_daily_limit`, scans per UTC day across all users
- `SCAN_FREE_DAILY_CEILING_MICROS` (`10000000` = $10, range 0–1,000,000,000) — **initial default only** (#531): seeds `breaker.free_daily_ceiling_micros`, the service-wide AI spend per UTC day (micro-dollars, all tiers counted) above which Free-tier scans pause with `503 ai_budget_reached`. Premium continues (decision #544). `0` pauses every Free scan
- `SCAN_FREE_DAILY_LIMIT` (`5`, range 0–1000), `SCAN_FREE_MONTHLY_LIMIT` (`5`, range 0–100,000), `SCAN_PREMIUM_DAILY_LIMIT` (`30`, range 0–1000), `SCAN_PREMIUM_MONTHLY_LIMIT` (`300`, range 0–100,000) — **initial defaults only** (#528): they seed `quota.free.daily`, `quota.free.monthly`, `quota.premium.daily` and `quota.premium.monthly`. Launch values (OD-1): every user is Free, 5 scans a day and 5 a month. `0` refuses every scan for that tier
- `SCAN_PREMIUM_ENHANCED_MONTHLY_LIMIT` (`20`, range 0–10,000) — **initial default only** (#533): seeds `quota.premium.enhanced_monthly`, enhanced scans per Premium user per UTC calendar month on top of the shared allowance. Enhanced scans are Premium-only, so there is no Free equivalent. `0` refuses every enhanced scan (`402 enhanced_limit_reached`)
- `ENHANCED_SCAN_FREE_CREDITS` — **no longer used** since #533 (the lifetime enhanced-scan credit balance is gone); still accepted so an existing environment loads, and removed in #540
- `SCAN_DAILY_LIMIT_PER_USER` — **no longer applied to `/scan`** since #528 (the per-user daily cap is now `quota.<tier>.daily`); read only by the legacy `scan_quota` module until #540 removes it
- `SCAN_MAX_IMAGE_PIXELS` (`40000000`, range 1,000,000–100,000,000) — seed for `scan.max_image_pixels`; decompression-bomb guard: an upload whose width × height is above this gets `413 image_too_large_pixels` before it is decoded
- `SCAN_NORMALISE_CONCURRENCY` (`1`, range 1–8) — how many scan images are normalised (decoded and resized, in worker threads) at once per process. Each can briefly hold a few hundred MB for a 40 MP image, so this bounds peak memory; requests over the limit wait for a slot. Peak is roughly 160–350 MB per 40 MP image, times this value, times the uvicorn worker count; keep it at 1 on a 512 MB instance and raise it only with more memory. Stays env (not in `ai_settings`): it sizes a process-wide `anyio.CapacityLimiter` created at import, so a database change could not take effect without a restart
- `AI_SETTINGS_CACHE_TTL_SECONDS` (`30`, range 1–300) — how long each process (uvicorn worker) reuses the `ai_settings` rows it last read
- `AI_SETTINGS_MAX_STALE_SECONDS` (`300`, range 1–3600, and never below `AI_SETTINGS_CACHE_TTL_SECONDS`; the app refuses to boot otherwise) — when a read of `ai_settings` fails, how old the cached rows may be and still be used; past this, scans return `503 ai_unavailable`
- `SCAN_MAX_TOKENS` (`2048`, range 1–16384) and `SCAN_TIMEOUT_SECONDS` (`45`, range 1–120) — reply cap and timeout for the vision call (the timeout is also the gateway's overall deadline for the whole provider call; past it the scan is refunded with `503 scan_unavailable`)
- `SCAN_MODEL` (`gpt-4o-mini`) and `SCAN_ENHANCED_MODEL` (`gpt-4o`) — must be an allowed OpenAI model (list in `backend/app/ai/models_allowlist.py`); the app names the env var and refuses to boot otherwise. This is deliberate (#552): a bad value stops the whole API, not just scans. Before removing a model from the allowlist, check that no environment (prod, dev, preview) still sets it, and that no `ai_settings` row still names it (`python -m app.ai.settings list`; a row naming a removed model is ignored with a warning)

AI settings (`ai_settings` table; no release or restart needed):

- Keys: `scan.standard.enabled` (all scans), `scan.enhanced.enabled`, `scan.standard.provider`, `scan.enhanced.provider` (`openai` or `anthropic`), `scan.standard.model`, `scan.enhanced.model`, `scan.max_image_pixels` (1,000,000–100,000,000). Models must be in the provider's allow-list.
- **Switching a scan feature to Claude (#532).** Set `ANTHROPIC_API_KEY` on the service first, then run `python -m app.ai.settings switch scan.standard anthropic --by <you>` (and/or `scan.enhanced`). It writes the provider and model rows together; the model defaults to the registry's per-provider model (`claude-haiku-5-5` for standard, `claude-sonnet-5-5` for enhanced; `--model` picks another allow-listed Anthropic model). Every process picks it up within `AI_SETTINGS_CACHE_TTL_SECONDS`. Roll back with `python -m app.ai.settings switch scan.standard openai --by <you>`. `set scan.standard.provider anthropic` on its own is refused, because the stored model is still an OpenAI one. Check with `python -m app.ai.settings list`, then confirm a new `ai_usage` row with `model = 'claude-haiku-5-5'` after a scan. **Production stays on `openai`** until the data-handling question (#549) and the owner actions in #542 are resolved; staging may switch now.
- Quota keys (#528): `scan.global_daily_limit` (1–1,000,000; `503 scan_capacity` when today's scans across all users reach it), `quota.free.daily` and `quota.premium.daily` (0–1000 scans per user per UTC day), `quota.free.monthly` and `quota.premium.monthly` (0–100,000 per UTC calendar month). Standard and enhanced scans share these allowances (one counter per user per window). These are the dials to cut spend quickly, e.g. `python -m app.ai.settings set quota.free.daily 0 --by <you>`. An invalid stored value is ignored with a warning and the env default is used.
- Cost ceiling (#531): `breaker.free_daily_ceiling_micros` (0–1,000,000,000 micro-dollars per UTC day; default $10). Once today's reserved plus settled AI spend (all tiers) would pass it, Free scans get `503 ai_budget_reached` until UTC midnight; Premium scans continue within their own allowances and the count cap. Sentry gets one warning per day at 50, 75 and 90 % of it (fingerprint `ai-breaker:<day>:<pct>`, sent as `["ai-breaker", "<day>", "<pct>"]`; thresholds fixed in code). Check that the Sentry project has an alert rule that notifies on these events before relying on them. E.g. `python -m app.ai.settings set breaker.free_daily_ceiling_micros 0 --by <you>` pauses Free scans at once. Today's totals are in the `ai_spend_daily` row for the UTC date.
- Enhanced scans (#533): `quota.premium.enhanced_monthly` (0–10,000 per Premium user per UTC month), `scan.enhanced.zero_result_threshold` (0–20, default 3), `scan.enhanced.zero_result_window_hours` (1–720, default 24) and `scan.enhanced.zero_result_action` (`charge`, default, or `throttle`). An enhanced scan that finds no books is free while the user has fewer than the threshold such free scans in the window; after that further empty enhanced scans are charged (`charge`) or enhanced scans are refused with `429 enhanced_throttled` until the window clears (`throttle`). Threshold `0` charges every empty enhanced scan (`charge`) or refuses every enhanced scan (`throttle`). The three zero-result keys have no env seed (code defaults). Sentry gets at most one warning per user per window when they reach the threshold (fingerprint `["ai-enhanced-zero-results", "<action>"]`, no user data); add an alert rule for it. E.g. `python -m app.ai.settings set scan.enhanced.zero_result_action throttle --by <you>`.
- Change them from a shell with the app's `DATABASE_URL` (e.g. the Render shell): `python -m app.ai.settings list`, `python -m app.ai.settings get scan.standard.enabled`, `python -m app.ai.settings set scan.standard.enabled false --by <you>`. `set` validates against the same bounds and records `updated_at` and `updated_by` (`--by`, else `$USER`, else `cli`); a bad value exits non-zero and writes nothing. Editing the row directly in the database also works, but skips validation. An invalid provider, model or pixel cap is then ignored with a warning and the default is used; an invalid `*.enabled` value (anything but JSON `true`/`false`, e.g. `"false"`, `0` or `null`) is treated as **disabled**, with a warning.
- Deleting an `*.enabled` row re-enables per `SCAN_ENABLED`/`SCAN_ENHANCED_ENABLED` (a missing row falls back to the env default); use `set … false` to disable.
- The cache is per process (per uvicorn worker). A change reaches every process within `AI_SETTINGS_CACHE_TTL_SECONDS` (30 s). With two instances running (zero-downtime deploys), or several workers, they can disagree for up to that long.
- If the table cannot be read and a process has never read it, scans return `503 ai_unavailable`. A process that has read it before keeps using the last value it read, with a warning, while that value is at most `AI_SETTINGS_MAX_STALE_SECONDS` (300 s) old; after that it logs an error and scans return `503 ai_unavailable` until a read succeeds.

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

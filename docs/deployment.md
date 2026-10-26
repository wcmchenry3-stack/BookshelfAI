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
- `AI_ANTHROPIC_BASE_URL` (#539) — **must stay unset in production and staging.** It exists only so the nightly load run (`.github/workflows/perf-ai.yml`) can point the Claude adapter at a local fake provider. Unset means `https://api.anthropic.com`. Only `localhost`, `127.0.0.1` or `::1` (with a valid port, no credentials, query or fragment) is accepted, whatever `ENVIRONMENT` says, so a deployment that forgot to set `ENVIRONMENT` still cannot send the API key and photos to another host. In a hardened environment (any `ENVIRONMENT` except `development` and `test`, so staging and production) **any non-empty value makes the app fail at boot.** The generic `ANTHROPIC_BASE_URL` is ignored. Check that the service does not have it set when you add a variable.
- `GOOGLE_BOOKS_API_KEY`
- `OPENAI_MAX_TOKENS` (optional)

Every scan limit and dial (default, bounds, setting, what the user sees, where it is counted) is listed in one place in [`scan-limits.md`](scan-limits.md); `python -m app.ai.settings list` shows them grouped by area with the current value, default and bounds.

Scan dials (optional; defaults shown; change in the dashboard and restart, no release needed — out-of-range values fail at boot):

- `SCAN_ENABLED` (`true`), `SCAN_ENHANCED_ENABLED` (`true`), `SCAN_MAX_IMAGE_PIXELS`, `SCAN_MODEL`, `SCAN_ENHANCED_MODEL` — **initial defaults only** (#526). They seed the `ai_settings` table on first boot (see "AI settings" below); after that, changing the env var does nothing for a key that already has a row. They are also the fallback for a key whose row is missing
- `SCAN_GLOBAL_DAILY_LIMIT` (`2000`, range 1–1,000,000) — **initial default only** (#528): seeds `scan.global_daily_limit`, scans per UTC day across all users
- `SCAN_FREE_DAILY_CEILING_MICROS` (`10000000` = $10, range 0–1,000,000,000) — **initial default only** (#531): seeds `breaker.free_daily_ceiling_micros`, the service-wide AI spend per UTC day (micro-dollars, all tiers counted) above which Free-tier scans pause with `503 ai_budget_reached`. Premium continues (decision #544). `0` pauses every Free scan
- `SCAN_FREE_DAILY_LIMIT` (`5`, range 0–1000), `SCAN_PREMIUM_DAILY_LIMIT` (`5`, range 0–1000; `30` before #577) — **initial defaults only** (#528): since #577 they seed `quota.free.found_daily` and `quota.premium.found_daily` (scans that found books; the retired `quota.<tier>.daily`). Launch values (OD-1): every user is Free, 5 scans a day. `0` refuses every scan for that tier
- `SCAN_FREE_NO_MATCH_DAILY_LIMIT` (`5`, range 0–1000), `SCAN_PREMIUM_NO_MATCH_DAILY_LIMIT` (`5`, range 0–1000) — **initial defaults only** (#577): they seed `quota.free.no_match_daily` and `quota.premium.no_match_daily`, the photos per user per UTC day where the AI found no book that do not use a scan
- `SCAN_FREE_MONTHLY_LIMIT`, `SCAN_PREMIUM_MONTHLY_LIMIT`, `SCAN_PREMIUM_ENHANCED_MONTHLY_LIMIT` — **removed** (#582, #594): there is no monthly limit. See "Upgrade notes" below
- `ENHANCED_SCAN_FREE_CREDITS`, `SCAN_DAILY_LIMIT_PER_USER` — **removed** (#533, #528, #540): the lifetime enhanced-scan credit grant and the old per-user daily cap are gone (the caps are `quota.<tier>.found_daily` and `quota.<tier>.no_match_daily` above). See "Upgrade notes" below
- `UNVERIFIED_BOOKS_PER_USER_MAX` (`200`, range 1–10,000) — the most private unverified library entries one user can have (#616, `POST /wishlist/unverified` answers `409 unverified_limit_reached` above it). Read at startup, so a change needs a restart
- `SCAN_MAX_IMAGE_PIXELS` (`40000000`, range 1,000,000–100,000,000) — seed for `scan.max_image_pixels`; decompression-bomb guard: an upload whose width × height is above this gets `413 image_too_large_pixels` before it is decoded
- `SCAN_NORMALISE_CONCURRENCY` (`1`, range 1–8) — how many scan images are normalised (decoded and resized, in worker threads) at once per process. Each can briefly hold a few hundred MB for a 40 MP image, so this bounds peak memory; requests over the limit wait for a slot. Peak is roughly 160–350 MB per 40 MP image, times this value, times the uvicorn worker count; keep it at 1 on a 512 MB instance and raise it only with more memory. Stays env (not in `ai_settings`): it sizes a process-wide `anyio.CapacityLimiter` created at import, so a database change could not take effect without a restart
- `AI_SETTINGS_CACHE_TTL_SECONDS` (`30`, range 1–300) — how long each process (uvicorn worker) reuses the `ai_settings` rows it last read
- `AI_SETTINGS_MAX_STALE_SECONDS` (`300`, range 1–3600, and never below `AI_SETTINGS_CACHE_TTL_SECONDS`; the app refuses to boot otherwise) — when a read of `ai_settings` fails, how old the cached rows may be and still be used; past this, scans return `503 ai_unavailable`
- `SCAN_MAX_TOKENS` (`2048`, range 1–16384) and `SCAN_TIMEOUT_SECONDS` (`45`, range 1–120) — reply cap and timeout for the vision call (the timeout is also the gateway's overall deadline for the whole provider call; past it the scan is refunded with `503 scan_unavailable`)
- `SCAN_MODEL` (`gpt-4o-mini`) and `SCAN_ENHANCED_MODEL` (`gpt-4o`) — must be an allowed OpenAI model (list in `backend/app/ai/models_allowlist.py`); the app names the env var and refuses to boot otherwise. This is deliberate (#552): a bad value stops the whole API, not just scans. Before removing a model from the allowlist, check that no environment (prod, dev, preview) still sets it, and that no `ai_settings` row still names it (`python -m app.ai.settings list`; a row naming a removed model is ignored with a warning)

AI settings (`ai_settings` table; no release or restart needed):

- Keys: `scan.standard.enabled` (all scans), `scan.enhanced.enabled`, `scan.standard.provider`, `scan.enhanced.provider` (`openai` or `anthropic`), `scan.standard.model`, `scan.enhanced.model`, `scan.max_image_pixels` (1,000,000–100,000,000). Models must be in the provider's allow-list.
- **Switching a scan feature to Claude (#532).** Set `ANTHROPIC_API_KEY` on the service first, then run `python -m app.ai.settings switch scan.standard anthropic --by <you>` (and/or `scan.enhanced`). It writes the provider and model rows together; the model defaults to the registry's per-provider model (`claude-haiku-5-5` for standard, `claude-sonnet-5-5` for enhanced; `--model` picks another allow-listed Anthropic model). Every process picks it up within `AI_SETTINGS_CACHE_TTL_SECONDS`. Roll back with `python -m app.ai.settings switch scan.standard openai --by <you>`. `set scan.standard.provider anthropic` on its own is refused, because the stored model is still an OpenAI one. Check with `python -m app.ai.settings list`, then confirm a new `ai_usage` row with `model = 'claude-haiku-5-5'` after a scan. **Production stays on `openai`** until the data-handling question (#549) and the owner actions in #542 are resolved; staging may switch now.
- Quota keys (#528): `scan.global_daily_limit` (1–1,000,000; `503 scan_capacity` when today's scans across all users reach it), `quota.<tier>.found_daily` and `quota.<tier>.no_match_daily` (0–1000 per user per UTC day, #577). Every scan needs a found unit; a standard scan that finds no book moves to no-match while it has room. Standard and enhanced scans share the found allowance. `quota.free.daily` and `quota.premium.daily` are retired (#577): the CLI refuses them, they are not seeded and a stored row is ignored, except that an operator-set value (a row not written by `seed`) is carried over to `found_daily` when no `found_daily` row exists yet (at the first boot of #577 the seed writes it). Remove the old rows with a later contract migration. There is no monthly limit (#582): `quota.free.monthly`, `quota.premium.monthly` and `quota.premium.enhanced_monthly` are retired, the CLI refuses them as unknown keys and migration 0018 (#594) deleted the stored rows. What bounds a month: each user's daily limit, `scan.global_daily_limit`, the daily cost ceiling below (a worst-case month for Free is about 31 × it) and the monthly spend limit in the provider console (#542; keep it set). Monthly spend and call counts are in `ai_usage_daily`. These are the dials to cut spend quickly, e.g. `python -m app.ai.settings set quota.free.found_daily 0 --by <you>`. An invalid stored value is ignored with a warning and the env default is used.
- Cost ceiling (#531): `breaker.free_daily_ceiling_micros` (0–1,000,000,000 micro-dollars per UTC day; default $10). Once today's reserved plus settled AI spend (all tiers) would pass it, Free scans get `503 ai_budget_reached` until UTC midnight; Premium scans continue within their own allowances and the count cap. Sentry gets one warning per day at 50, 75 and 90 % of it (fingerprint `ai-breaker:<day>:<pct>`, sent as `["ai-breaker", "<day>", "<pct>"]`; thresholds fixed in code). Check that the Sentry project has an alert rule that notifies on these events before relying on them. E.g. `python -m app.ai.settings set breaker.free_daily_ceiling_micros 0 --by <you>` pauses Free scans at once. Today's totals are in the `ai_spend_daily` row for the UTC date.
- Enhanced scans (#533): Premium-only, limited by the shared daily allowance (no enhanced monthly allowance since #582; #583 adds a daily Sonnet counter). `scan.enhanced.zero_result_threshold` (0–20, default 3), `scan.enhanced.zero_result_window_hours` (1–720, default 24) and `scan.enhanced.zero_result_action` (`charge`, default, or `throttle`). An enhanced scan that finds no books is recorded while the user has fewer than the threshold such recorded scans in the window; after that nothing more is recorded (`charge`) or enhanced scans are refused with `429 enhanced_throttled` until the window clears (`throttle`). Every enhanced scan uses the daily unit, empty or not. Threshold `0` records nothing (`charge`) or refuses every enhanced scan (`throttle`). The three zero-result keys have no env seed (code defaults). Sentry gets at most one warning per user per window when they reach the threshold (fingerprint `["ai-enhanced-zero-results", "<action>"]`, no user data); add an alert rule for it. E.g. `python -m app.ai.settings set scan.enhanced.zero_result_action throttle --by <you>`.
- Scan records (#585): `scan_records.enabled` (bool, default `true`; the kill switch: `false` stops both lookups and saves, so every scan takes the normal, charged path and repeats are no longer free; an invalid value counts as disabled), `scan_records.retention_days` (1–30, default 30; days a record is kept and served; a lowered value applies to reads at once and to the next daily purge) and `scan_records.max_per_user` (1–10,000, default 500; oldest evicted first). No env seed (code defaults). E.g. `python -m app.ai.settings set scan_records.enabled false --by <you>`. Old records are purged by the daily cleanup task. To watch failure rates, count the `scan_record event=…` log lines (e.g. `event=saved … enrich_done=false` for records left without an enrichment step, `event=repeat … in_library=0` for repeats whose books are not in the library, and `attempt=N`, #603, for which try of the request it was: 1 first, 2–6 the app's retries); they carry no user id or content. The retry rate of all scans is counted from `ai_usage.attempt` (one row per request that reached the provider or was a repeat).
- Request limit (#586): `scan.requests_per_user_daily` (1–10,000, default 100): `/scan` requests per user per UTC day, new photo or repeat, allowed or refused later. At the limit the user gets `429 scan_requests_daily_limit` until UTC midnight. Raise it for a user base that legitimately scans more; it is an abuse control, not a cost limit (the found allowance and the global cap bound AI calls).
- Change them from a shell with the app's `DATABASE_URL` (e.g. the Render shell): `python -m app.ai.settings list`, `python -m app.ai.settings get scan.standard.enabled`, `python -m app.ai.settings set scan.standard.enabled false --by <you>`. `set` validates against the same bounds and records `updated_at` and `updated_by` (`--by`, else `$USER`, else `cli`); a bad value exits non-zero and writes nothing. Editing the row directly in the database also works, but skips validation. An invalid provider, model or pixel cap is then ignored with a warning and the default is used; an invalid `*.enabled` value (anything but JSON `true`/`false`, e.g. `"false"`, `0` or `null`) is treated as **disabled**, with a warning.
- Deleting an `*.enabled` row re-enables per `SCAN_ENABLED`/`SCAN_ENHANCED_ENABLED` (a missing row falls back to the env default); use `set … false` to disable.
- The cache is per process (per uvicorn worker). A change reaches every process within `AI_SETTINGS_CACHE_TTL_SECONDS` (30 s). With two instances running (zero-downtime deploys), or several workers, they can disagree for up to that long.
- If the table cannot be read and a process has never read it, scans return `503 ai_unavailable`. A process that has read it before keeps using the last value it read, with a warning, while that value is at most `AI_SETTINGS_MAX_STALE_SECONDS` (300 s) old; after that it logs an error and scans return `503 ai_unavailable` until a read succeeds.

### Required and optional production settings

All settings are read from the environment by `Settings` in `backend/app/core/config.py` (the full list, with defaults and bounds, is there). **A blank value is treated as not set, so the default applies** (#648). This covers the process environment and `.env`. The exceptions, which stay boot errors, are the settings with no default or a security-sensitive one:

| Name | Required? | Default | Allowed values / range | Blank |
|---|---|---|---|---|
| `DATABASE_URL` | required | none | Postgres URL; port 6543 (Supabase transaction pooler) refused | error (no default) |
| `JWT_PRIVATE_KEY` | required when `ENVIRONMENT` is not `development`/`test` | empty | non-empty secret (the catalog-token key derives from it) | blank = unset, then the hardened check fails |
| `ENVIRONMENT` | optional | `development` | any; anything but `development`/`test` is hardened | **boot error** (never silently development) |
| `TRUSTED_HOSTS` | optional | `["*"]` | JSON list of hosts, e.g. `["api.example.com"]` | **boot error** |
| `CORS_ORIGINS` | optional | none | JSON list; `*` refused | blank = default |
| `SCAN_MODEL`, `SCAN_ENHANCED_MODEL` | optional | `gpt-4o-mini`, `gpt-4o` | allowed OpenAI model (`backend/app/ai/models_allowlist.py`) | blank = default |
| `SCAN_MAX_TOKENS` | optional | `2048` | 1-16384 | blank = default |
| `SCAN_TIMEOUT_SECONDS` | optional | `45` | 1-120 | blank = default |
| `SCAN_ENABLED`, `SCAN_ENHANCED_ENABLED` | optional | `true` | `true`/`false` | blank = default (`true`; blank never disables) |
| `TURNSTILE_REQUIRED` | optional | `true` | `true`/`false` | blank = default (`true`) |
| `ORIGIN_AUTH_SECRET` | optional | empty (check off) | 32+ printable ASCII characters | blank = unset (check off) |
| `AI_ANTHROPIC_BASE_URL` | must stay unset | empty | local host only; any value fails boot when hardened | blank = unset |

Other optional settings (rate limits, scan allowances, pool sizes, `AI_SETTINGS_*`, and so on) are listed below and in `config.py`; every one of them treats blank as its default. To turn a boolean or a limit off, set an explicit value (`false`, `0`), never an empty one.

Check an environment without starting the app or touching the database:

```bash
cd backend && python -m app.core.config_check
# OK            (exit 0)
# or a list of "NAME: reason" lines, never values (exit 1)
```

It builds `Settings` as boot does and runs the database-free checks (such as `JWT_PRIVATE_KEY` in a hardened environment).

### Database

`bookshelf-db` — Postgres, `basic-256mb` plan, Oregon. Connection string is auto-injected into the API service.

### Release process (backend)

0. Before merging a release: open a shell on the production `bookshelf-api` service in Render and run `cd backend && python -m app.core.config_check`. It must print `OK`; fix any listed variable first. (Alembic imports the settings, so an invalid environment otherwise fails the deploy at `alembic upgrade head`.)
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

**Rolling back past the unverified-books release (#616, migration `0016`) needs `alembic downgrade 0015` first, and that deletes data.** Users' private unverified library entries (`user_books` rows with `custom_book_id` set, and their `user_custom_books` rows) are removed by the downgrade. Do not skip it: the previous release's `UserBookRead` cannot serialise a library row whose `book` is `None`, so with those rows still in the table `GET /user-books`, `POST /wishlist` and the other endpoints that return a library entry would answer `500` for every user who has one. Run `alembic downgrade 0015` (against the production database, before or while switching traffic to the older deploy), then roll the deploy back. The entries cannot be restored by rolling forward again; take a database backup first if they matter.

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

## Versioning and release notes (Release Please)

`release-please.yml` runs on every push to `main` (the shared `called-release-please.yml`). It reads `release-please-config.json` and `.release-please-manifest.json` at the repo root.

- **Version scheme:** one version for the whole repo, tracked in `version.txt` (`release-type: simple`). Pre-launch versions are `0.x`. The 2026-10-26 release (#641) is `0.1.0`, and commits before it are ignored (`bootstrap-sha`). Until 1.0, `feat:` bumps the minor version and `fix:` bumps the patch. A `!` breaking change also bumps only the minor (`bump-minor-pre-major`). 1.0.0 is the public launch, cut with a `feat!:` commit or by editing the manifest.
- **Flow:** merge `dev` into `main`. Release Please then opens (or updates) a release PR, `chore(main): release X.Y.Z`, that bumps `version.txt` and writes `CHANGELOG.md` from the Conventional Commit titles. Merging that PR creates the `vX.Y.Z` tag and GitHub Release.
- **App store versions are not touched.** `frontend/package.json`, `app.json` and the native iOS and Android version numbers are still set by hand for store builds.
- **Repository settings it needs:** Settings → Actions → General → Workflow permissions → **"Allow GitHub Actions to create and approve pull requests"**. Without it, Release Please (and `sync-main-to-dev.yml`) fail with `Resource not accessible by integration`.
- **The release PR's CI.** A PR opened with the default `GITHUB_TOKEN` does not trigger other workflows, so its CI does not start on its own. Push a commit to the release branch, or close and reopen the PR, if a required check is waiting. Alternatively, have the workflow use a fine-grained token (contents and pull-requests: write), passed as the called workflow's `release-token` secret.

## Upgrade notes

**#594 (contract step for #582, no monthly scan limit).** Migration 0018 deletes the `ai_settings` rows `quota.free.monthly`, `quota.premium.monthly` and `quota.premium.enhanced_monthly` and drops `ai_inflight.monthly_taken` and `ai_inflight.enhanced_taken`. The settings class no longer has `SCAN_FREE_MONTHLY_LIMIT`, `SCAN_PREMIUM_MONTHLY_LIMIT` or `SCAN_PREMIUM_ENHANCED_MONTHLY_LIMIT`.

- **Local `.env` files: delete those three lines.** A key in the `.env` file that matches no setting is refused, so the backend will not start while they are there. A process environment variable that matches no setting is ignored, so Render (and any shell export) keeps booting; remove them from the Render environment anyway.
- The previous release (#582) names the two dropped columns in its lease acquisition statement, so while the pre-deploy migration has run and the new code is not yet live, scans fail to take their lease and are refused (503/500). The window is one deploy. Ship this release after #582 is live everywhere.
- Downgrading 0018 recreates the two columns but cannot bring back the deleted `ai_settings` rows; the release before #582 seeds any missing quota key from its env defaults at boot.
- `ai_quota` rows with `window_kind = 'month'` are not touched: nothing reads them and the daily cleanup purges them after 13 months.

**#540 (contract step for #528 and #533, legacy scan counters).** Migration 0019 drops the table `scan_usage_daily` and the column `users.enhanced_scan_credits`, with their data. The settings class no longer has `ENHANCED_SCAN_FREE_CREDITS` or `SCAN_DAILY_LIMIT_PER_USER`, and the daily cleanup no longer purges `scan_usage_daily`.

- **Local `.env` files: delete `ENHANCED_SCAN_FREE_CREDITS` and `SCAN_DAILY_LIMIT_PER_USER` if present.** Same rule as #594: a key in the `.env` file that matches no setting is refused (the backend will not start); a process environment variable that matches no setting is ignored, so Render keeps booting. Remove both from the Render environment anyway.
- The previous release maps `users.enhanced_scan_credits` on the `User` model, so every query that loads a user names the column. Between the pre-deploy migration and the new code taking traffic, requests that load the user (every authenticated endpoint, so also `/auth/refresh` and the scan) fail with a server error. The window is one deploy. The old release's daily cleanup also deletes from `scan_usage_daily` once a day (at startup plus 60 s, then every 24 h); during the window that step fails and is logged, and nothing else uses the table.
- Downgrading 0019 recreates the table (empty) and the column (every user gets the default 5) but cannot restore the data; nothing reads either one.

## Common release gotchas

| Symptom | Cause | Fix |
|---|---|---|
| Render deploy fails at `alembic upgrade head` | migration error | fix the migration, re-push; the old version keeps serving traffic |
| `/health/db` returns 503 after deploy | DB unreachable | check `bookshelf-db` status in Render dashboard |
| Web site serving old JS | browser cached `/*` | `Cache-Control: no-cache` is set on HTML but browsers sometimes cache aggressively — hard-reload |
| iOS build fails at `ci_post_clone.sh` | Node version mismatch (rule #12) | see `claude/ios-ci.md` |
| Android `bundleRelease` fails with CMake error | JDK 24+ on path | `export JAVA_HOME=$(/usr/libexec/java_home -v 17)` (rule #22, preflight in `settings.gradle`) |
| App Store / Play Console reject version bump | `versionCode` not incremented | bump `versionCode` (Android) / `CFBundleVersion` (iOS) every submission |

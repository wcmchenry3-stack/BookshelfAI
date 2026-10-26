# Scan limits and dials

One place to see every limit, allowance, cap and dial that can stop, slow or shape a scan, with its default, bounds, the setting that controls it, what the user sees when it is hit, and where it is counted. Values are the code defaults on `dev`; an `ai_settings` row, once it exists, overrides the default (see "How to read and change these").

The AI gateway write-up (#541, `AI-GATEWAY.md`, not written yet) will link here instead of repeating these tables. Behaviour in detail is in [`api.md`](api.md) (the `/scan` refusals and their order), [`architecture.md`](architecture.md) (the scan flow) and [`deployment.md`](deployment.md) (env vars). This page is the index; if it disagrees with the code, the code wins, and `backend/tests/unit/test_scan_limits_doc.py` fails when a registry key is missing here.

## How to read and change these

- **See everything, grouped by area, with each key's current value, default and bounds:**
  `cd backend && python -m app.ai.settings list` (from a shell with the service's `DATABASE_URL`, e.g. the Render shell). One key: `get <key>`.
- **Change an `ai_settings` key:** `python -m app.ai.settings set <key> <value> --by <you>`. The value is validated against the bounds below and the change reaches every process within `AI_SETTINGS_CACHE_TTL_SECONDS` (30 s). No deploy, no restart. A provider and its model are changed together with `switch <feature> <provider>`.
- **Env seeds only apply on first boot.** For every key with an env var in the tables, the env var (or the code default) only creates the `ai_settings` row when it is missing. After that the row is the source of truth and changing the env var does nothing. A key whose row is missing or invalid falls back to the same env/code default; an invalid `*.enabled` row fails closed (treated as `false`).
- **Needs a deploy or restart:** anything not marked `ai_settings` in the tables below: the `rate_limit_*` settings and `unverified_books_per_user_max` / `SCAN_NORMALISE_CONCURRENCY` / the cache TTLs (env, read at startup, restart), and every constant in code (body caps, 5 MB image cap, JPEG settings, lease grace). The client-side values (retry schedule, offline queue, retention) ship with the app, so they need an app release.
- **Window:** every "per day" below is a UTC day (resets at 00:00 UTC) unless it says otherwise.
- **Adding a limit:** add its row here in the same PR. The drift test fails for a registry key or a `rate_limit_*` setting that is not mentioned in this file. A story that adds or changes a limit also adds rows to the limits matrix (`backend/tests/limits/`, see [`claude/testing.md`](claude/testing.md)).

Columns: **Limit** · **What it limits** · **Applies to** (Free / Premium / everyone) · **Default** · **Setting** (`ai_settings` key, or env var / settings field) · **Bounds** · **User sees when hit** · **Tracked in**.

## 1. Allowances (per user, per day)

Everyone is Free until purchases exist (#583). A scan needs a found unit before the provider is called; a standard scan whose answer has no book gives the found unit back and uses a no-match unit instead (#577).

| Limit | What it limits | Applies to | Default | Setting | Bounds | User sees when hit | Tracked in |
|---|---|---|---|---|---|---|---|
| Found scans, Free | Scans per user per UTC day in which the provider found books; standard and enhanced share it | Free | 5 | `quota.free.found_daily` (seed `SCAN_FREE_DAILY_LIMIT`) | 0..1,000 (0 refuses every scan) | `429 daily_scan_limit` with `Retry-After` to UTC midnight; UI `scan:dailyLimit` | `ai_quota`, feature `scan` |
| Found scans, Premium | Same, Premium tier | Premium | 5 | `quota.premium.found_daily` (seed `SCAN_PREMIUM_DAILY_LIMIT`) | 0..1,000 | as above | `ai_quota`, feature `scan` |
| No-match scans, Free | Standard scans per user per day where the provider answered properly but found no book; they do not use a found unit while this lasts | Free | 5 | `quota.free.no_match_daily` (seed `SCAN_FREE_NO_MATCH_DAILY_LIMIT`) | 0..1,000 | Nothing separate: when it is full the empty scan keeps its found unit (so the found limit above is what the user hits) | `ai_quota`, feature `scan.no_match` |
| No-match scans, Premium | Same, Premium tier | Premium | 5 | `quota.premium.no_match_daily` (seed `SCAN_PREMIUM_NO_MATCH_DAILY_LIMIT`) | 0..1,000 | as above | `ai_quota`, feature `scan.no_match` |
| Concurrent scans | One paid scan in flight per user | everyone | 1 (fixed) | code (`ai_inflight` lease, scan timeout + 30 s grace) | not configurable | `429 scan_in_progress`, `Retry-After: 2`; UI `scan:stillProcessing` | `ai_inflight` |

## 2. Request and rate limits

| Limit | What it limits | Applies to | Default | Setting | Bounds | User sees when hit | Tracked in |
|---|---|---|---|---|---|---|---|
| `/scan` requests per day | Every authenticated `/scan` request past the kill switches (new photo or repeat, allowed or refused later; nothing is given back) | everyone | 100 | `scan.requests_per_user_daily` (#586) | 1..10,000 | `429 scan_requests_daily_limit` with `Retry-After` to UTC midnight; UI `scan:requestsDailyLimit` | `ai_quota`, feature `scan.requests` (not an AI call, not in the global count) |
| `/scan` per minute | Request rate on `POST /scan`, per client | everyone | `10/minute` | `rate_limit_scan` (env `RATE_LIMIT_SCAN`) | slowapi rate string | `429` with slowapi's own body (no `detail` code) | in-memory limiter |
| Auth per minute | `/auth/google`, `/auth/refresh`, `/auth/logout` | everyone | `5/minute` | `rate_limit_auth` | rate string | `429` (slowapi) | in-memory limiter |
| Book search per minute | `GET /books/search` | everyone | `30/minute` | `rate_limit_books_search` | rate string | `429` (slowapi) | in-memory limiter |
| Writes per minute | Library mutations (`/user-books`, `/wishlist/...`) | everyone | `60/minute` | `rate_limit_writes` | rate string | `429` (slowapi) | in-memory limiter |
| Reads per minute | `GET /auth/me`, `/scan/usage`, `/user-books` | everyone | `120/minute` | `rate_limit_reads` | rate string | `429` (slowapi) | in-memory limiter |
| Health per minute | `/health`, `/health/db` | everyone | `60/minute` | `rate_limit_health` | rate string | `429` (slowapi) | in-memory limiter |

The `rate_limit_*` settings are env vars read at startup (change in the Render dashboard, restart). The limiter is per process, so with several workers the real ceiling is the rate times the number of workers.

## 3. Global caps and spend

| Limit | What it limits | Applies to | Default | Setting | Bounds | User sees when hit | Tracked in |
|---|---|---|---|---|---|---|---|
| Global daily scans | Scans per UTC day across all users (found and no-match counters summed; not the request counter) | everyone | 2,000 | `scan.global_daily_limit` (seed `SCAN_GLOBAL_DAILY_LIMIT`) | 1..1,000,000 | `503 scan_capacity`; UI "Scanning is paused" (`scan:scanPaused`) | `ai_quota` (sum of the day) |
| Free daily spend ceiling | AI spend per UTC day, all tiers counted, above which Free scans pause (the circuit breaker, #531). Premium is not paused (#544) | Free | 10,000,000 micro-dollars ($10) | `breaker.free_daily_ceiling_micros` (seed `SCAN_FREE_DAILY_CEILING_MICROS`) | 0..1,000,000,000 (1,000,000 = $1; 0 pauses every Free scan) | `503 ai_budget_reached`; UI `scan:scanPaused` | `ai_spend_daily` (reserved + settled micros) |
| Estimated cost per call | What each scan reserves against the ceiling before the provider is called | everyone | 10,000 micros standard, 30,000 enhanced | code (`app/ai/registry.py`, `est_cost_ceiling_micros`) | not configurable | counts toward the ceiling above | `ai_spend_daily`; actuals in `ai_usage` |
| Scanning switches | Master switch for all scans; switch for enhanced only | everyone / Premium | on | `scan.standard.enabled` (seed `SCAN_ENABLED`), `scan.enhanced.enabled` (seed `SCAN_ENHANCED_ENABLED`) | `true` or `false` (anything else reads as `false`) | `503 scan_disabled` / `503 enhanced_scan_disabled`; UI `scan:scanPaused` | `ai_settings` |
| Settings unreadable | The settings table cannot be read and nothing recent is cached (fail closed) | everyone | cache 30 s, stale up to 300 s | env `AI_SETTINGS_CACHE_TTL_SECONDS` (1..300), `AI_SETTINGS_MAX_STALE_SECONDS` (1..3600, not below the TTL) | as listed | `503 ai_unavailable`; UI `scan:scanPaused` | n/a |

Provider and model choice are dials too, not limits: `scan.standard.provider`, `scan.enhanced.provider` (a provider in `ALLOWED_MODELS`) and `scan.standard.model`, `scan.enhanced.model` (a model in that provider's allow-list). Use `switch` to change a pair. The model call itself is bounded by `SCAN_MAX_TOKENS` (2,048, 1..16,384), `SCAN_TIMEOUT_SECONDS` (45, 1..120; a timeout is `503 scan_unavailable`, refunded) and `SCAN_MAX_BOOKS` (15 candidates).

## 4. Enhanced scans

Enhanced scans are Premium-only (#533) and use the shared found allowance in section 1 (no separate daily counter yet; #583 adds one).

| Limit | What it limits | Applies to | Default | Setting | Bounds | User sees when hit | Tracked in |
|---|---|---|---|---|---|---|---|
| Premium only | Who may request an enhanced scan; checked before anything is reserved | Free refused | n/a | code (`entitlements.tier_for`) | n/a | `403 enhanced_requires_premium`; UI `scan:enhancedRequiresPremium` | n/a |
| Zero-result threshold | Enhanced scans with no books recorded per user per window before the action applies | Premium | 3 | `scan.enhanced.zero_result_threshold` | 0..20 | Nothing at the threshold with `charge`; one Sentry warning per user per window | `ai_enhanced_zero_results` (kept 30 days) |
| Zero-result window | Sliding window the count covers | Premium | 24 hours | `scan.enhanced.zero_result_window_hours` | 1..720 | n/a | `ai_enhanced_zero_results` |
| Zero-result action | At the threshold: `charge` (stop recording; every scan uses the daily unit) or `throttle` (refuse enhanced scans until the window clears) | Premium | `charge` | `scan.enhanced.zero_result_action` | `charge` or `throttle` | With `throttle`: `429 enhanced_throttled` with `Retry-After`; UI `scan:enhancedThrottled` / `scan:enhancedThrottledLater` | `ai_enhanced_zero_results` |

## 5. Caching and repeat photos

A repeat of the exact same photo (same normalised bytes, same user) is served from a scan record: no provider call, no found or no-match unit, one `cache_hit` usage row costing 0 (#585). It still counts as a `/scan` request in section 2.

| Limit | What it limits | Applies to | Default | Setting | Bounds | User sees when hit | Tracked in |
|---|---|---|---|---|---|---|---|
| Scan records on/off | Lookup and save of scan records; off means every scan takes the normal, charged path | everyone | on | `scan_records.enabled` | `true` or `false` (invalid reads as `false`) | Repeats cost a unit again | `scan_records` |
| Record retention | Days a record is kept and served; older ones are purged daily | everyone | 30 days | `scan_records.retention_days` | 1..30 | A repeat after that is a new, charged scan | `scan_records.created_at` |
| Records per user | Records kept per user; the oldest are evicted first | everyone | 500 | `scan_records.max_per_user` | 1..10,000 | An evicted photo is a new, charged scan | `scan_records` (per `user_id`) |

## 6. Retries and offline queue (client side)

These live in the app (`frontend/lib/scanErrors.ts`, `frontend/lib/scanJob.ts`), not in `ai_settings`; changing one needs an app release.

| Limit | What it limits | Applies to | Default | Setting | Bounds | User sees when hit | Tracked in |
|---|---|---|---|---|---|---|---|
| Retry schedule | Waits before attempts 2, 3, 4 and 5 of a photo after `503 scan_unavailable` (the only retried outcome; #604) | everyone | 30 s, 10 min, 1 h, 4 h | `SCAN_RETRY_DELAYS_MS` (code) | fixed; 5 attempts total (`MAX_SCAN_ATTEMPTS`) | After attempt 5 the photo is failed: `scan:scanFailedFinal` with a Retry button | scan job in the device queue |
| Attempt number sent | The `attempt` form field, recorded only (never used for limits) | everyone | 1..6 | `SCAN_ATTEMPT_MIN/MAX` in `app/api/scan.py` | 1..6 | `422` outside the range | `ai_usage` ledger |
| Queue size | Photos waiting to upload on the device | everyone | 50 | `MAX_QUEUE_SIZE` (code) | fixed | `scan:queueFull` / `scan:queueFullLimited` | device storage |
| Failed-photo retention | How long a failed photo stays in the queue before it is dropped (#605) | everyone | 7 days | `FAILED_SCAN_RETENTION_MS` (code) | fixed | The failed photo disappears | device storage |
| `scan_in_progress` retry | Wait before the app retries a `429 scan_in_progress` | everyone | `Retry-After` (2 s if missing), capped at 10 s | `DEFAULT_IN_PROGRESS_RETRY_SECONDS`, `MAX_IN_PROGRESS_RETRY_SECONDS` (code) | fixed | `scan:stillProcessing` | n/a |

## 7. Uploads and images

| Limit | What it limits | Applies to | Default | Setting | Bounds | User sees when hit | Tracked in |
|---|---|---|---|---|---|---|---|
| Image file size | Bytes of the uploaded image, read in chunks | everyone | 5 MB | `MAX_IMAGE_BYTES` in `app/api/scan.py` (code) | fixed | `413`, `Image must be 5MB or smaller` | n/a |
| `/scan` request body | Whole multipart body on `POST /scan` (image cap plus 1 MB of framing), counted as it arrives, chunked or not (#557) | everyone | 6 MB | `MAX_SCAN_REQUEST_BYTES` (code) | fixed | `413`, `Request body too large` | n/a |
| Other request bodies | Every other route | everyone | 10 MB | `RequestSizeLimitMiddleware.MAX_BODY_SIZE` (code) | fixed | `413`, `Request body too large`; a bad `Content-Length` is `400` | n/a |
| Decoded pixels | Width x height read from the header before decoding (decompression-bomb guard) | everyone | 40,000,000 | `scan.max_image_pixels` (seed `SCAN_MAX_IMAGE_PIXELS`) | 1,000,000..100,000,000 | `413 image_too_large_pixels` | n/a |
| Normalised size | Long edge the provider gets, re-encoded as JPEG quality 85 | everyone | 1,568 px | `max_image_long_edge_px` in `app/ai/registry.py`; `JPEG_QUALITY` in `app/ai/images.py` (code) | fixed | n/a (the photo is shrunk) | n/a |
| Concurrent normalisations | Images decoded and resized at once, per process (bounds memory) | everyone | 1 | env `SCAN_NORMALISE_CONCURRENCY` | 1..8 | Extra requests wait | n/a |
| File type | Extension, MIME and magic bytes: JPEG, PNG, WebP, HEIC/HEIF | everyone | n/a | code | n/a | `415` | n/a |

## 8. Other

| Limit | What it limits | Applies to | Default | Setting | Bounds | User sees when hit | Tracked in |
|---|---|---|---|---|---|---|---|
| Unverified books per user | Private "unverified" library entries one user can hold (#616) | everyone | 200 | `unverified_books_per_user_max` (env `UNVERIFIED_BOOKS_PER_USER_MAX`) | 1..10,000 | `409 unverified_limit_reached`; UI `addUnverified.limitReached` | `user_custom_books` |
| Turnstile | Every `/scan` needs a valid Cloudflare Turnstile token | everyone | required | env `TURNSTILE_REQUIRED` | `true` or `false` | `400` (missing token) / `403` (failed) | n/a |

## Retired (do not use)

Not live limits; kept only so an old row or `.env` still loads.

- `quota.free.monthly`, `quota.premium.monthly`, `quota.premium.enhanced_monthly` (#582): there is no monthly limit. The CLI rejects these keys, seeding no longer writes them and a stored row is ignored on read. Migration 0018 (#594) deleted the stored rows, and the env fields `SCAN_FREE_MONTHLY_LIMIT`, `SCAN_PREMIUM_MONTHLY_LIMIT` and `SCAN_PREMIUM_ENHANCED_MONTHLY_LIMIT` are gone (a local `.env` that still lists one fails to boot; see the upgrade notes in `docs/deployment.md`).
- `quota.free.daily`, `quota.premium.daily` (#577): became `quota.<tier>.found_daily`.
- `SCAN_DAILY_LIMIT_PER_USER` and `ENHANCED_SCAN_FREE_CREDITS`: removed in #540, with the `scan_usage_daily` table and the `users.enhanced_scan_credits` column (migration 0019). A local `.env` that still lists one fails to boot; see the upgrade notes in `docs/deployment.md`.

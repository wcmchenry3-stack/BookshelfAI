# External integrations

Every third-party service Bookshelf talks to, what role it plays, and where to look in the code. For architectural context see [`architecture.md`](architecture.md); for API surface see [`api.md`](api.md).

## Google OAuth

**Role:** identity provider. Users sign in with Google; Bookshelf exchanges the Google ID token for its own access + refresh pair.

**Client IDs:** three — one per platform — so the same backend serves web, iOS, and Android:

| Env var | Platform | Google Cloud "Application type" |
|---|---|---|
| `GOOGLE_CLIENT_ID` | Web | Web application |
| `GOOGLE_IOS_CLIENT_ID` | iOS | iOS |
| `GOOGLE_ANDROID_CLIENT_ID` | Android | Android |

**Verification flow** (`backend/app/auth/google.py`):
1. Frontend obtains an ID token from the Google SDK on-device.
2. Frontend calls `POST /auth/google` with that token in the body.
3. Backend gets Google's JWKS from `https://www.googleapis.com/oauth2/v3/certs` via the in-process `JwksCache` (`backend/app/auth/jwks_cache.py`, reusable for other providers): fetched with a 5s timeout, cached for the response's `Cache-Control: max-age` (1 h fallback, 24 h cap, 60 s floor). A token with an unknown `kid` forces one refresh (throttled to once per 60 s) to pick up key rotation; concurrent sign-ins share a single in-flight fetch. If a refresh fails, cached keys keep being used until 24 h past expiry (`MAX_STALE_SECONDS`), with a warning logged; after that sign-in fails (401). With nothing cached, a failed fetch is remembered for 60 s and further sign-ins fail fast (401) without re-fetching.
4. `authlib.jose.jwt.decode()` validates the token: signature via JWKS, `iss == "https://accounts.google.com"`, `aud ∈ [all three configured client IDs]`, expiry.
5. `email_verified` claim must be `True`.
6. Email must be in `ALLOWED_EMAILS` (comma-separated env var) — the single-user lockdown.

**Why all three audiences are accepted:** each platform's OAuth SDK issues tokens with its own `aud` claim. Accepting all three in one check means the backend doesn't have to care which client signed in.

## Anthropic Claude (vision)

**Role:** book identification. Given a photo (one cover, a stack, or a shelf of spines), returns every readable book (up to `SCAN_MAX_BOOKS`) with a confidence and optional ISBN hints. Anthropic is the **only** AI provider (#532 added it; the previous provider was removed in #538 on 2026-11-22, there is no rollback to it). Production has run on Claude since 2026-11-22 (after the #549 decision); see [`RENDER.md`](RENDER.md#scans-run-on-claude-538). An `ai_settings` row that still names a removed provider or model is ignored with a warning and the registry default is used.

**SDK:** official `anthropic` Python SDK, pinned in `backend/requirements.txt` (1.x, built on `httpx2`); `client.messages.create` only
**Endpoint:** `https://api.anthropic.com`, pinned in the adapter. The only way to change it is the validated `AI_ANTHROPIC_BASE_URL` setting (#539, read once at boot into `settings.ai_anthropic_base_url`), which exists for the nightly load run's local fake provider; it accepts only `localhost` / `127.0.0.1` / `::1` in any environment, and in staging and production any value fails boot. With the override set, the SDK client does not follow redirects, so a local endpoint cannot bounce the API key and photo to another host
**Operator environment:** the base URL and the API key are passed explicitly, so the SDK's own reading of `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` and saved profiles is not used. **The generic `ANTHROPIC_BASE_URL` is deliberately ignored** by the app and the SDK: a developer's shell often exports it for Claude Code or a gateway, and honouring it would send `ANTHROPIC_API_KEY` to that host. Only the project-specific `AI_ANTHROPIC_BASE_URL` (validated, as above) can change the endpoint. Some environment settings do still apply to the SDK client, for example: the HTTP library's proxy and TLS variables (`HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY`, `SSL_CERT_FILE` / `SSL_CERT_DIR`), `ANTHROPIC_CUSTOM_HEADERS` (extra request headers) and `ANTHROPIC_LOG`. **Keep `ANTHROPIC_LOG` unset in production** (and the `anthropic` / `httpx2` loggers above DEBUG): at `debug` the SDK logs the full request options, including the base64 image, which breaks the rule that logs carry only lengths and hashes. Keep the others unset on the API service unless a proxy or custom CA is intended.
**Models:** `claude-haiku-5-5` (`scan.standard`), `claude-sonnet-5-5` (`scan.enhanced`): fixed IDs (Anthropic publishes no dated snapshot for them, #548), from `registry.PROVIDER_MODELS`
**Key env var:** `ANTHROPIC_API_KEY` (without it a scan answers 503 `scan_unavailable`; the app still boots)
**Timeout / max tokens / effort:** the feature spec's (`SCAN_TIMEOUT_SECONDS`, `SCAN_MAX_TOKENS`; effort `low` standard, `medium` enhanced)
**Source:** `backend/app/ai/providers/anthropic_vision.py`, called only through the AI gateway
**Settings:** the kill switches and limits are `ai_settings` rows (`python -m app.ai.settings list`); see [deployment.md](deployment.md) and [scan-limits.md](scan-limits.md)

**Request:** the system prompt (`SYSTEM_PROMPT` in the adapter) with a `cache_control` breakpoint. The cached prefix is about 900 tokens and is reused: on dev (2026-11-22) the ledger showed `cache_write_tokens = 907` on the first scan and `cache_read_tokens = 907` on the next; structured output (`output_config.format`) for `{books: [{title, author, confidence, isbn_13, isbn_10}]}`; the normalised JPEG as a base64 image block. No sampling parameters and no prefill.

**Error handling:**
- `stop_reason: refusal` → empty result, ledger `refused`, reservation kept
- `stop_reason: max_tokens` → complete books parsed, ledger `truncated`; with no complete book, ledger `malformed`, 503, refunded (#572)
- Any other stop reason, or an `end_turn` reply that is missing, not JSON, the wrong shape, or a non-empty list in which every book is structurally broken (not an object, a title or author missing, not a string or over 500 characters) → ledger `malformed`, 503, refunded (#572); a book with an empty or blank title or author is dropped, as before
- 429 / 500 / 503 / 529 → up to 2 retries (jittered backoff, honours `retry-after`, bounded by the timeout), then `ScanUnavailableError` → 503, refunded
- Other 4xx / 502 / 504 → `ScanUnavailableError` → 503, refunded (4xx also logged at error level)
- Timeout → `ProviderTimeoutError` → 503, refunded, ledger `timeout`
- Connection never established → `ProviderConnectError` → 503, refunded
- `ANTHROPIC_API_KEY` missing → 503, refunded, error log; the app still boots
- Invalid ISBN (digit count or checksum) → dropped; title and author kept

**Cost note:** list prices per 1M tokens (in `backend/app/ai/prices.py`): Haiku 5.5 $0.10 input / $0.50 output / $0.01 cache read / $0.125 cache write, for prompts up to 100K tokens (above that $0.50 / $2.50; scans are about 3K tokens, and a longer prompt logs a warning); Sonnet 5.5 $2.00 / $10.00 / $0.20 / $2.50. The Haiku cache-read price follows Anthropic's 0.1×-input rule and must be checked on the live pricing page before production enablement. The usage ledger records the real cost per call.

**Evaluation (#536):** which model serves each tier is decided from the labelled, on-demand eval in `backend/eval/ai_scan/` (`.github/workflows/ai-eval.yml`): the owner's shelf photos from a private R2 bucket, through the same image normalisation and adapter as `/scan`, scored deterministically, under a hard spend cap. Re-run it when a prompt, model or `max_tokens` changes. See [testing.md](testing.md#ai-scan-evaluation-536-on-demand).

## Google Books API

**Role:** metadata enrichment — covers, descriptions, page counts, ISBNs.

**Endpoint:** `https://www.googleapis.com/books/v1/volumes`
**Key env var:** `GOOGLE_BOOKS_API_KEY` (optional — API works unauthenticated at a lower quota)
**Timeout:** 10 seconds
**Source:** `backend/app/services/google_books.py`; the GET itself (10 s timeout, raise on a non-2xx status, JSON body) is `get_json` in `backend/app/services/_lookup_http.py`, shared with Open Library (#814)

**Methods used:**
- `search(title, author, limit=1, score=None)` — `q=intitle:"<title>" inauthor:"<author>"` (quoted phrases; the author part only when there is one), `maxResults=limit` (at most 5). With `score`, returns the highest-scoring volume with a positive score, or None when none scores above 0; without `score`, the first volume. Enrichment calls it with the read title, the read author's surname, `limit=5` and a score of how well each volume fits the reading (#743): for a candidate without a valid ISBN-13, and when the ISBN lookup found nothing or another book.
- `search_query(q, limit=3)` — free-text, backs `GET /books/search`.
- `search_by_isbn(isbn)` — `q=isbn:<isbn>`, `maxResults=1`. The first lookup for a candidate with a valid ISBN-13; its volume is used only when it fits the reading or lists the ISBN under the same main title (#743).

**Fields consumed:** `volumeInfo.{title, authors, description, imageLinks, industryIdentifiers}`. Cover URL picks the largest available (`large` → `medium` → `small` → `thumbnail`) and rewrites `http://` to `https://`.

**Fallback behaviour:** if Google Books returns no items or the call fails, enrichment still produces a candidate — it just won't have a cover / description. `/books/search` raises `502` on Google Books errors; `/scan` absorbs them silently.

**Catalog data comes from the providers (#602).** The book's title, author, IDs, cover, description and edition details are taken from these provider records, never from the vision model's reading of a photo or from the request body. When Open Library and Google Books both match, the Google Books volume is used only if it agrees with the Open Library doc (a shared ISBN, or the same title and first-author surname, where a differing subtitle may only be an edition note); otherwise nothing from it is used. An ISBN is kept only if a record used lists it. A result neither provider matched still shows in the app, but it has no catalog token and can't be added to the wishlist (`422 book_not_in_catalog`). See [security.md → Shared catalog trust model](security.md).

## Open Library

**Role:** work-level identity (`work_id`) — the primary dedup key.

**Endpoint:** configurable via `OPEN_LIBRARY_BASE_URL` (default `https://openlibrary.org`)
**No API key** — Open Library is free + unauthenticated
**Timeout:** 10 seconds
**Source:** `backend/app/services/open_library.py` (request through `backend/app/services/_lookup_http.py`, as Google Books)

**Methods used:**
- `search(title, author, limit=1, score=None)` — `/search.json?title=...&author=...&fields=key,title,subtitle,author_name,isbn,subject&limit=<limit>` (at most 5; `author` only when there is one). With `score`, returns the highest-scoring doc with a positive score, or None when none scores above 0; without `score`, the first doc. Called like the Google Books `search` above (#743).
- `search_by_isbn(isbn)` — same endpoint, `isbn=<isbn>` param, `limit=1`

**Cost (#743, #1003).** A scan candidate costs up to 6 provider calls, 3 per provider (was 2 in all): an ISBN lookup, a title search, and, for a reading with a subtitle whose title search answered with nothing fitting, a main-title search. So up to 90 per 15-book scan, half of them (45) to Google Books. An ISBN-13 with a bad check digit (which the vision model often invents) is not looked up, and a lookup that failed is never retried. Each call is cut off after 8 s, with a 20 s deadline for the whole scan's enrichment (the fallback is skipped when under 8 s of it remain); see [architecture.md](architecture.md) step 3. Google Books quota: at most 3 requests per candidate, so up to 45 per scan (was 15 before #743, 30 after it) and, at the default `scan.global_daily_limit` of 2,000 scans a day, up to 90,000 a day from new scans (was 30,000). Repeats whose enrichment step is missing redo it outside that cap, and `GET /books/search` adds up to 10 per search (one free-text query plus up to 3 per result). Typical scans need far fewer: one Google Books request per candidate when the ISBN record fits or there is no ISBN.

**Why Open Library over Google Books for dedup:** Open Library's `work_id` identifies the conceptual work (*Dune*), not a specific edition. Google Books' `volume_id` is also work-level but less consistent — Open Library has cleaner work/edition separation. See [data-model.md → Dedup strategy](data-model.md#dedup-strategy--why-work_id-not-isbn).

## Sentry

**Role:** error + performance monitoring across backend and frontend.

### Backend

**Init:** `backend/app/main.py` — `sentry_sdk.init()` with `StarletteIntegration` + `FastApiIntegration`. Disabled when `SENTRY_DSN` is empty.

**Sample rates:**
- `traces_sample_rate`: 0.2 in production, 1.0 elsewhere
- `send_default_pii`: `False`

**Context enrichment** (`backend/app/core/sentry_context.py`):
- Tags attached to every event: `request_id`, `endpoint`, `method`
- User identity via `set_sentry_user(user_id)` — ID only, no email/name (PII)

**Spend alerts (#531):** the cost breaker (`backend/app/ai/breaker.py`) sends one `warning` event per day at 50, 75 and 90 % of the Free daily AI budget, fingerprint `ai-breaker:<day>:<pct>` (sent as `["ai-breaker", "<day>", "<pct>"]`), with tags `feature`, `model`, `status` and the day's totals as extras. They carry no user and no request data (an isolated scope plus an event processor strips the request block, user, breadcrumbs and request contexts). Sentry only stores them: an alert rule on these events (e.g. fingerprint/message starts with `ai-breaker`) must route them to the owner before they can be relied on.

**Repeated empty enhanced scans (#533):** when a user's uncharged zero-result enhanced scans inside `scan.enhanced.zero_result_window_hours` reach `scan.enhanced.zero_result_threshold` (`backend/app/ai/quota/sentry_warnings.py`, `send_zero_result_warning`), one `warning` event is sent, message `ai-enhanced: zero-result enhanced scans reached the threshold`, fingerprint `["ai-enhanced-zero-results", "<action>"]`, tags `feature=scan.enhanced` and `action` (`charge` or `throttle`), extras `count`, `threshold` and `window_hours`. Never a user id, email, image, image hash or content: it is captured through the same stripped, emptied scopes as the spend alerts (`backend/app/ai/alerts.py`). At most one per user per window: the row that fires it is marked `warned = true` in `ai_enhanced_zero_results` in the same transaction, and no new warning is sent for that user while a marked row is inside the window, even if older rows age out and the count reaches the threshold again. Only the request holding the user's lease can claim it (the lease row is locked until the settle commits), so two overlapping settles cannot both send it. None with threshold 0; none for a user whose in-window count was already above a threshold that was then lowered (they are still charged or throttled); and none if the settle that committed the claim lost its acknowledgement (the retry records nothing, so that window's warning is lost rather than duplicated). Like the spend alerts, it needs a Sentry alert rule (e.g. fingerprint starts with `ai-enhanced-zero-results`) to reach anyone.

**No-match ceiling (#577):** when a user's no-match scans (standard scans where the AI answered but found no book) reach the day's ceiling `quota.<tier>.no_match_daily` (`backend/app/ai/quota/sentry_warnings.py`, `send_no_match_ceiling_warning`), one `warning` event is sent after the settle commits, message `ai-quota: a user's no-match scans reached the daily ceiling`, fingerprint `["ai-no-match-ceiling"]`, tag `tier` (`free` or `premium`), extra `ceiling`. At most one per user per UTC day: only the move that brings the count to the ceiling sends it. Never a user id, email, image, image hash or content (the same emptied, stripped scopes as above). Per-user counts are in `ai_quota` (quota key `scan.no_match`). The alert rule is #578.

### Frontend

**Init:** `frontend/lib/sentry.ts`. Imported as the **first import in `app/_layout.tsx`** (rule #13) — the module self-initializes so the native crash reporter is up before any other module can throw.

**Filtering (#701):** `beforeSend` drops two narrow, user-environment web network signatures (font-loader `<n>ms timeout exceeded`, stackless `NetworkError` DOMException); see `docs/claude/frontend.md` ("Sentry noise filter").

**Scrubbing (#736):** after the filter, `beforeSend` replaces an unhandled AxiosError's own exception entry with a sanitized `ApiError`, and rebuilds `extra`, `contexts`, `logentry`, `message`, `event.request` and exception mechanism data: auth/cookie/secret-named keys (`authorization`, `cookie`, `access_token`, `password`, `api_key`, ...) at any depth and Axios/XHR-shaped `config`/`request`/`response` `headers`/`data` are redacted, and bearer tokens, raw JWTs, `Authorization`/`Cookie` header lines and token query params are masked in strings (all linear-time). `beforeBreadcrumb` applies the same scrub to breadcrumb `message`/`data`. It fails closed: a field that cannot be scrubbed is dropped, and an event that cannot be copied is dropped (`null`). Web caveat: `event.request`'s page URL gets query-param redaction only; no tokens travel in page URLs today. Code: `frontend/lib/sentryScrub.ts`; details in `docs/claude/frontend.md` ("Sentry event scrubbing").

**API failures (#724):** `lib/api.ts` reports unexpected API failures (403, 5xx) once per (method, path template, status, detail) per app session, with tags `http.status`, `http.method`, `api.path` and (allowlisted codes only) `api.detail`; no tokens, headers or bodies. See `docs/claude/frontend.md` ("API error reporting").

**Breadcrumb categories:**
- `sentry.filter` — an event was dropped by the noise filter
- `app.lifecycle` — Sentry init completed, root layout evaluated, foreground/background transitions
- `scan` — camera capture started, photo saved, scan started, retry attempts
- `ui.lifecycle` — screen mounts (from Sentry's auto-instrumentation)
- `http` — every API call (via Sentry's fetch/xhr instrumentation)

**Key tags used in scan flow** (`frontend/contexts/ScanJobContext.tsx`): `feature=scan`, `action=execute_scan`, `jobType=image|text`.

## Cloudflare

**Zone:** `buffingchi.com`
**Role:** DNS + WAF + Turnstile CAPTCHA

### Turnstile (bot protection on `/scan`)

**Siteverify endpoint:** `https://challenges.cloudflare.com/turnstile/v0/siteverify`
**Key env var:** `TURNSTILE_SECRET_KEY` (leave blank to skip the check in dev)
**Timeout:** 5 seconds
**Source:** `backend/app/api/scan.py:_verify_turnstile`

When `TURNSTILE_REQUIRED=true` (the default; production runs `false` until the app's site key ships), every `POST /scan` must include `cf-turnstile-response` as a multipart form field. Missing → 400, invalid → 403. A Turnstile outage falls through to False (request rejected) — the 5s timeout prevents it from blocking the endpoint indefinitely.

**Client (#740):** `EXPO_PUBLIC_TURNSTILE_SITE_KEY` (public site key) and, native only, `EXPO_PUBLIC_TURNSTILE_BASE_URL` (default: the origin of `EXPO_PUBLIC_API_URL`). The app gets a fresh token per request from an invisible widget (hidden WebView on iOS/Android, Cloudflare's script on web) and sends it as `cf-turnstile-response`; without a site key, or when the widget times out (10 s) or fails, the request goes without one. Details: [`security.md` → Turnstile on /scan](security.md#turnstile-on-scan). Re-enabling the check: [`RENDER.md` → Re-enabling `TURNSTILE_REQUIRED`](RENDER.md#re-enabling-turnstile_required).

### WAF + Bot Fight Mode

Zone has Bot Fight Mode enabled — it 403s GitHub Actions runner IPs (Azure ranges) on free tier and **cannot be skipped via WAF custom rules**. See [architecture.md → ADR: CI health checks bypass Cloudflare](architecture.md#adr-ci-health-checks-bypass-cloudflare) for the full workaround (direct Render origin + `_HealthExemptTrustedHost`).

### Client IP and origin lockdown

With `TRUSTED_PROXY_MODE=cloudflare`, `app/core/client_ip.py` takes the client IP from a single valid `CF-Connecting-IP` header so slowapi keys rate limits by the real client, not Cloudflare's edge IP. `ORIGIN_AUTH_SECRET` turns on the origin lockdown, so only requests that came through the edge (plus `/health`) reach the API. See [security.md → Client IP and origin lockdown](security.md#client-ip-and-origin-lockdown).

## Supabase Storage (account export files)

**Role:** private storage of the "Export my data" files (#694): one JSON document per request at `exports/<export id>-<upload ms>.json` in the bucket `account-exports` (`EXPORT_BUCKET`) of the same Supabase project as the database. Users download through a signed URL valid 7 days; nothing else reads the bucket. #874 (debug photos) will reuse the client.

**Client:** `backend/app/services/object_storage.py` (`ObjectStorage`: `upload` with `x-upsert`, `signed_url`, bulk `delete`, `list`) over the Storage REST API (`<SUPABASE_URL>/storage/v1`) with httpx; no Supabase SDK.
**Settings:** `SUPABASE_URL` (https, nothing after the host), `SUPABASE_SERVICE_ROLE_KEY` (secret, env only, sent only to `SUPABASE_URL`, never logged), `EXPORT_BUCKET` (default `account-exports`, must be private). Either of the first two unset = `from_settings()` is `None` = the export is off (`POST /account/export` answers 503 `export_not_configured`). Owner setup: [`RENDER.md`](RENDER.md#account-export-storage-supabase-storage-694).
**Failure behaviour:** 15 s timeout per request, 3 tries (a transport error, a timeout, a 429 or a 5xx is retried after 0.5 s, then 1 s; any other status is final), then `StorageError` carrying the operation and status only (never the URL, the key or a token). The export worker records it as `storage_<operation>_<status>` and retries the export after its 10-minute lease, at most 3 attempts; the daily purge logs a storage failure, keeps the rows whose file it could not delete for the next run, stops its loop and still lets the sweep run. The purge holds no transaction or row lock across a storage call.
**Limits:** at most one open or ready export per user per 24 h (or 3 failed ones), each about 1.4 MB per 1,000 books, kept 7 days; a bulk delete takes at most 1,000 paths and the purge sends at most 100 per call. Storage use against the plan: [`storage-and-retention.md`](storage-and-retention.md#account-export-files-supabase-storage).
**Tests:** `httpx.MockTransport` only (`tests/unit/test_object_storage.py`; a fake bucket with signed-token expiry in `tests/integration/test_account_export_db.py`).

## Resend (transactional email)

**Role:** sends the account export's link (#694), from `Bookshelf <EMAIL_FROM>` on the verified domain `mail.buffingchi.com`, `Reply-To: EMAIL_REPLY_TO` (#678). The first email the backend sends; account deletion (#291) will reuse the sender.

**Client:** `backend/app/services/email.py` (`send(Message, idempotency_key=...)`), `POST https://api.resend.com/emails` with httpx, HTML and text bodies. Localised strings: `backend/app/email_locales/<locale>.json` (the app's 13 locales).
**Settings:** `RESEND_API_KEY` (secret, env only; unset = `email.configured()` false = the export is off), `EMAIL_FROM` (default `noreply@mail.buffingchi.com`), `EMAIL_REPLY_TO`. Domain and key setup: [`RENDER.md`](RENDER.md#email-resend).
**Idempotency:** every send carries `Idempotency-Key: account-export-<export id>-<link expiry>` (one key per signed link). Resend answers a repeat with the same key and payload within 24 hours with the first response and sends nothing; the same key with a different payload is `409 invalid_idempotent_request`. The worker records the signed link before the first send and rebuilds the identical email on a retry, so an email whose response was lost is never sent twice. It records `email_accepted_at` when Resend accepted the send, or may have (a timeout or a 5xx on any try of the send, not only the last, or a `409 concurrent_idempotent_requests`: another request with the key is in flight), and reads the 409 as "already sent" only then: Resend may keep the key of a request it refused, and a refused send must never mark an unsent export delivered. A recorded link with under an hour left is never emailed; the worker signs a new one (new key) instead.
**Failure behaviour:** 15 s timeout, the same 3 tries as storage, then `EmailError` with the status and Resend's error `name` only (never the address, the subject or a body). The export stays `emailing` and is retried after the lease.
**Limits:** one email per export, at most one export per user per day. Resend's own rate and monthly limits depend on the plan; at this volume they are not reached.
**Tests:** `httpx.MockTransport` only (`tests/unit/test_email_service.py`; a fake honouring `Idempotency-Key` in `tests/integration/test_account_export_db.py`).

## Render

**Role:** backend hosting (FastAPI + Postgres). Auto-deploys from the `dev` branch.

**Config:** `render.yaml` at repo root.

**Service:** `bookshelf-api` — Python, Oregon region, `starter` plan.
- Build: `pip install -r requirements.txt`
- Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT --no-proxy-headers`
- **Pre-deploy:** `alembic upgrade head` — migrations run before the new version takes traffic. A failing migration blocks the deploy (test migrations locally first).
- Health check path: `/health`
- Root dir: `backend/`

**Database:** Supabase Postgres (prod project; persistent branch `dev` for the dev service) through the session pooler, as the `bookshelf_app` role (#696); see [`RENDER.md`](RENDER.md#database-role). The Render Postgres `bookshelf-db` is suspended and no longer referenced.

**Env vars on Render:** configured in the Render dashboard (not in `render.yaml` for secrets). Expected:
- `DATABASE_URL` (Supabase session pooler URL as `bookshelf_app`; dashboard secret)
- `MIGRATION_DATABASE_URL` (owner DSN, used only by `alembic upgrade head`; dashboard secret)
- `ENVIRONMENT=production`
- `CORS_ORIGINS` (JSON array, no wildcards)
- `TRUSTED_HOSTS` (JSON array — **must include both the public domain and any direct-origin probe hostnames**)
- `GOOGLE_CLIENT_ID` + `GOOGLE_IOS_CLIENT_ID` + `GOOGLE_ANDROID_CLIENT_ID` + `GOOGLE_CLIENT_SECRET`
- `JWT_PRIVATE_KEY` + `JWT_PUBLIC_KEY`
- `ALLOWED_EMAILS`
- `ANTHROPIC_API_KEY`
- `GOOGLE_BOOKS_API_KEY`
- `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` (account export storage, #694) and `RESEND_API_KEY` (email)
- `SENTRY_DSN`
- `TURNSTILE_SECRET_KEY`

**TRUSTED_HOSTS gotcha:** production's `TRUSTED_HOSTS` is locked to the public domain. Any probe that hits the origin by its `*.onrender.com` hostname (CI, uptime monitors) gets rejected by `TrustedHostMiddleware` with HTTP 400 — unless the request hits `/health`, which is explicitly exempt via `_HealthExemptTrustedHost`. See [architecture.md → Auth + middleware](architecture.md#middleware-stack).

## Putting it together — the scan request

One `POST /scan` hits **five** external services:

1. **Cloudflare Turnstile** — verify the bot-check token (400/403 guard)
2. **Claude vision (Anthropic)** — identify every book in the photo → candidates with confidence + ISBN hints
3. **Google Books** — enrich each candidate with covers / descriptions / page counts
4. **Open Library** — resolve `work_id` for the primary dedup key
5. **Sentry** — breadcrumbs + error capture throughout

Each call is independently timed out (5s Turnstile, 45s Claude (`SCAN_TIMEOUT_SECONDS`), 10s Google Books, 10s Open Library) so one slow upstream can't block the request beyond its own budget.

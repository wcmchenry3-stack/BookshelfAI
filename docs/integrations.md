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
3. Backend fetches Google's JWKS from `https://www.googleapis.com/oauth2/v3/certs` (5s timeout).
4. `authlib.jose.jwt.decode()` validates the token: signature via JWKS, `iss == "https://accounts.google.com"`, `aud ∈ [all three configured client IDs]`, expiry.
5. `email_verified` claim must be `True`.
6. Email must be in `ALLOWED_EMAILS` (comma-separated env var) — the single-user lockdown.

**Why all three audiences are accepted:** each platform's OAuth SDK issues tokens with its own `aud` claim. Accepting all three in one check means the backend doesn't have to care which client signed in.

## OpenAI (gpt-4o-mini vision)

**Role:** book-cover identification. Given a photo, returns up to 3 candidates with confidence scores.

**Endpoint:** `https://api.openai.com/v1/chat/completions`
**Model:** `gpt-4o-mini`
**Key env var:** `OPENAI_API_KEY`
**Timeout:** 45 seconds (`SCAN_TIMEOUT_SECONDS`)
**Max tokens:** `SCAN_MAX_TOKENS` (default 2048), applied via the feature registry
**Source:** `backend/app/ai/providers/openai_vision.py`, called only through the AI gateway (`backend/app/ai/gateway.py`)

**Prompt strategy** — structured JSON output, no free text:
> You are a book identification assistant. Given a photo of a book cover, identify the book and return a JSON array of up to 3 candidates ranked by confidence. Each candidate must have title, author, confidence (0–1), isbn_13 (nullable), isbn_10 (nullable). Return ONLY a valid JSON array, no other text.

Image is base64-encoded and attached as a `data:image/jpeg;base64,...` URL. No file upload to OpenAI's Files API — the call is single-turn, no state.

**Error handling:**
- Timeout → `ScanUnavailableError` → endpoint returns `503 scan_unavailable`
- Non-2xx (including 429 rate limit) → `ScanUnavailableError` → 503
- Connection never established (`httpx.ConnectError`) → endpoint returns 503 `scan_unavailable` and refunds the daily-scan reservation
- No content, not JSON, the wrong shape (a bare list is still accepted), or a non-empty list in which every entry is structurally broken → ledger `malformed`, `503 scan_unavailable`, refunded (#572)
- Structurally broken entries (not an object, a title or author missing or not a string, a confidence that is not a number) are skipped individually; an empty title or author is kept, as before (a title-only lookup)
- `finish_reason: length` → ledger `truncated` with the parsed books; with none, `malformed` (503, refunded; #572)

**Cost note:** gpt-4o-mini is the cheapest vision-capable model. Scan cost is approximately $0.001 per image at current pricing. The `rate_limit_scan` bucket (10/min per client) caps abuse at the HTTP layer.

## Anthropic Claude (vision, #532)

**Role:** the same book identification as OpenAI, behind a per-feature setting. OpenAI stays listed above and stays the default until the labelled eval (#536) and the OpenAI removal (#538). Production enablement waits on the data-handling answer (#549) and the owner actions in #542.

**SDK:** official `anthropic` Python SDK, pinned in `backend/requirements.txt` (1.x, built on `httpx2`); `client.messages.create` only
**Endpoint:** `https://api.anthropic.com` (pinned in the adapter, so `ANTHROPIC_BASE_URL` cannot redirect it)
**Operator environment:** the base URL and the API key are passed explicitly, so `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` and saved profiles are not used. Some environment settings do still apply to the SDK client, for example: the HTTP library's proxy and TLS variables (`HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY`, `SSL_CERT_FILE` / `SSL_CERT_DIR`), `ANTHROPIC_CUSTOM_HEADERS` (extra request headers) and `ANTHROPIC_LOG`. **Keep `ANTHROPIC_LOG` unset in production** (and the `anthropic` / `httpx2` loggers above DEBUG): at `debug` the SDK logs the full request options, including the base64 image, which breaks the rule that logs carry only lengths and hashes. Keep the others unset on the API service unless a proxy or custom CA is intended.
**Models:** `claude-haiku-5-5` (`scan.standard`), `claude-sonnet-5-5` (`scan.enhanced`): fixed IDs (Anthropic publishes no dated snapshot for them, #548), from `registry.PROVIDER_MODELS`
**Key env var:** `ANTHROPIC_API_KEY` (only needed once a feature is switched to `anthropic`)
**Timeout / max tokens / effort:** the feature spec's (`SCAN_TIMEOUT_SECONDS`, `SCAN_MAX_TOKENS`; effort `low` standard, `medium` enhanced)
**Source:** `backend/app/ai/providers/anthropic_vision.py`, called only through the AI gateway
**Switch:** `python -m app.ai.settings switch scan.standard anthropic` (and back with `... switch scan.standard openai`); see [deployment.md](deployment.md)

**Request:** the OpenAI adapter's system prompt (unchanged wording) with a `cache_control` breakpoint (the prompt is about 300 tokens, under the 512-token cache minimum, so it most likely does not cache; confirm on staging from the ledger's cache token columns); structured output (`output_config.format`) for `{books: [{title, author, confidence, isbn_13, isbn_10}]}`; the normalised JPEG as a base64 image block. No sampling parameters and no prefill.

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

## Google Books API

**Role:** metadata enrichment — covers, descriptions, page counts, ISBNs.

**Endpoint:** `https://www.googleapis.com/books/v1/volumes`
**Key env var:** `GOOGLE_BOOKS_API_KEY` (optional — API works unauthenticated at a lower quota)
**Timeout:** 10 seconds
**Source:** `backend/app/services/google_books.py`

**Methods used:**
- `search(title, author)` — `q=intitle:<title>+inauthor:<author>`, `maxResults=1`. Primary enrichment lookup.
- `search_query(q, limit=3)` — free-text, backs `GET /books/search`.
- `search_by_isbn(isbn)` — `q=isbn:<isbn>`, `maxResults=1`. Called as a fallback when title+author doesn't resolve cleanly.

**Fields consumed:** `volumeInfo.{title, authors, description, imageLinks, industryIdentifiers}`. Cover URL picks the largest available (`large` → `medium` → `small` → `thumbnail`) and rewrites `http://` to `https://`.

**Fallback behaviour:** if Google Books returns no items or the call fails, enrichment still produces a candidate — it just won't have a cover / description. `/books/search` raises `502` on Google Books errors; `/scan` absorbs them silently.

**Catalog data comes from the providers (#602).** The book's title, author, IDs, cover, description and edition details are taken from these provider records, never from the vision model's reading of a photo or from the request body. When Open Library and Google Books both match, the Google Books volume is used only if it agrees with the Open Library doc (a shared ISBN, or the same main title and first-author surname); otherwise nothing from it is used. An ISBN is kept only if a record used lists it. A result neither provider matched still shows in the app, but it has no catalog token and can't be added to the wishlist (`422 book_not_in_catalog`). See [security.md → Shared catalog trust model](security.md).

## Open Library

**Role:** work-level identity (`work_id`) — the primary dedup key.

**Endpoint:** configurable via `OPEN_LIBRARY_BASE_URL` (default `https://openlibrary.org`)
**No API key** — Open Library is free + unauthenticated
**Timeout:** 10 seconds
**Source:** `backend/app/services/open_library.py`

**Methods used:**
- `search(title, author)` — `/search.json?title=...&author=...&fields=key,title,author_name,isbn,subject&limit=1`
- `search_by_isbn(isbn)` — same endpoint, `isbn=<isbn>` param

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

**Repeated empty enhanced scans (#533):** when a user's uncharged zero-result enhanced scans inside `scan.enhanced.zero_result_window_hours` reach `scan.enhanced.zero_result_threshold` (`backend/app/ai/quota.py`, `send_zero_result_warning`), one `warning` event is sent, message `ai-enhanced: zero-result enhanced scans reached the threshold`, fingerprint `["ai-enhanced-zero-results", "<action>"]`, tags `feature=scan.enhanced` and `action` (`charge` or `throttle`), extras `count`, `threshold` and `window_hours`. Never a user id, email, image, image hash or content: it is captured through the same stripped, emptied scopes as the spend alerts (`backend/app/ai/alerts.py`). At most one per user per window: the row that fires it is marked `warned = true` in `ai_enhanced_zero_results` in the same transaction, and no new warning is sent for that user while a marked row is inside the window, even if older rows age out and the count reaches the threshold again. Only the request holding the user's lease can claim it (the lease row is locked until the settle commits), so two overlapping settles cannot both send it. None with threshold 0; none for a user whose in-window count was already above a threshold that was then lowered (they are still charged or throttled); and none if the settle that committed the claim lost its acknowledgement (the retry records nothing, so that window's warning is lost rather than duplicated). Like the spend alerts, it needs a Sentry alert rule (e.g. fingerprint starts with `ai-enhanced-zero-results`) to reach anyone.

**No-match ceiling (#577):** when a user's no-match scans (standard scans where the AI answered but found no book) reach the day's ceiling `quota.<tier>.no_match_daily` (`backend/app/ai/quota.py`, `send_no_match_ceiling_warning`), one `warning` event is sent after the settle commits, message `ai-quota: a user's no-match scans reached the daily ceiling`, fingerprint `["ai-no-match-ceiling"]`, tag `tier` (`free` or `premium`), extra `ceiling`. At most one per user per UTC day: only the move that brings the count to the ceiling sends it. Never a user id, email, image, image hash or content (the same emptied, stripped scopes as above). Per-user counts are in `ai_quota` (quota key `scan.no_match`). The alert rule is #578.

### Frontend

**Init:** `frontend/lib/sentry.ts`. Imported as the **first import in `app/_layout.tsx`** (rule #13) — the module self-initializes so the native crash reporter is up before any other module can throw.

**Breadcrumb categories:**
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

When configured, every `POST /scan` must include `cf-turnstile-response` as a multipart form field. Missing → 400, invalid → 403. A Turnstile outage falls through to False (request rejected) — the 5s timeout prevents it from blocking the endpoint indefinitely.

### WAF + Bot Fight Mode

Zone has Bot Fight Mode enabled — it 403s GitHub Actions runner IPs (Azure ranges) on free tier and **cannot be skipped via WAF custom rules**. See [architecture.md → ADR: CI health checks bypass Cloudflare](architecture.md#adr-ci-health-checks-bypass-cloudflare) for the full workaround (direct Render origin + `_HealthExemptTrustedHost`).

### Client IP and origin lockdown

With `TRUSTED_PROXY_MODE=cloudflare`, `app/core/client_ip.py` takes the client IP from a single valid `CF-Connecting-IP` header so slowapi keys rate limits by the real client, not Cloudflare's edge IP. `ORIGIN_AUTH_SECRET` turns on the origin lockdown, so only requests that came through the edge (plus `/health`) reach the API. See [security.md → Client IP and origin lockdown](security.md#client-ip-and-origin-lockdown).

## Render

**Role:** backend hosting (FastAPI + Postgres). Auto-deploys from the `dev` branch.

**Config:** `render.yaml` at repo root.

**Service:** `bookshelf-api` — Python, Oregon region, `starter` plan.
- Build: `pip install -r requirements.txt`
- Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT --no-proxy-headers`
- **Pre-deploy:** `alembic upgrade head` — migrations run before the new version takes traffic. A failing migration blocks the deploy (test migrations locally first).
- Health check path: `/health`
- Root dir: `backend/`

**Database:** `bookshelf-db` — Postgres, Oregon region, connection string injected as `DATABASE_URL`.

**Env vars on Render:** configured in the Render dashboard (not in `render.yaml` for secrets). Expected:
- `DATABASE_URL` (auto, from the DB resource)
- `ENVIRONMENT=production`
- `CORS_ORIGINS` (JSON array, no wildcards)
- `TRUSTED_HOSTS` (JSON array — **must include both the public domain and any direct-origin probe hostnames**)
- `GOOGLE_CLIENT_ID` + `GOOGLE_IOS_CLIENT_ID` + `GOOGLE_ANDROID_CLIENT_ID` + `GOOGLE_CLIENT_SECRET`
- `JWT_PRIVATE_KEY` + `JWT_PUBLIC_KEY`
- `ALLOWED_EMAILS`
- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY` (only once a feature is switched to `anthropic`)
- `GOOGLE_BOOKS_API_KEY`
- `SENTRY_DSN`
- `TURNSTILE_SECRET_KEY`
- `OPENAI_MAX_TOKENS` (optional, default 512)

**TRUSTED_HOSTS gotcha:** production's `TRUSTED_HOSTS` is locked to the public domain. Any probe that hits the origin by its `*.onrender.com` hostname (CI, uptime monitors) gets rejected by `TrustedHostMiddleware` with HTTP 400 — unless the request hits `/health`, which is explicitly exempt via `_HealthExemptTrustedHost`. See [architecture.md → Auth + middleware](architecture.md#middleware-stack).

## Putting it together — the scan request

One `POST /scan` hits **five** external services:

1. **Cloudflare Turnstile** — verify the bot-check token (400/403 guard)
2. **OpenAI Vision** — identify the cover → 3 candidates with confidence + ISBN hints
3. **Google Books** — enrich each candidate with covers / descriptions / page counts
4. **Open Library** — resolve `work_id` for the primary dedup key
5. **Sentry** — breadcrumbs + error capture throughout

Each call is independently timed out (5s Turnstile, 15s OpenAI, 10s Google Books, 10s Open Library) so one slow upstream can't block the request beyond its own budget.

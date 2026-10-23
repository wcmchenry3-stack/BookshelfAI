# API reference

The full REST surface. For architectural context (middleware, auth model) see [`architecture.md`](architecture.md). For the data types returned in responses see [`data-model.md`](data-model.md).

**Base URL (prod):** `https://bookshelfapi.buffingchi.com`
**Interactive docs:** `http://localhost:8000/docs` (non-prod only — FastAPI auto-generated Swagger UI)

## Conventions

### Authentication

Every endpoint except `/auth/google`, `/auth/refresh`, `/health`, and `/health/db` requires a valid access token:

```
Authorization: Bearer <access_token>
```

Access tokens are JWT RS256, 24h lifetime (`JWT_EXPIRY_HOURS`). On expiry, the client calls `/auth/refresh` with its refresh token to get a new pair (old refresh token is revoked in the process — see [architecture.md](architecture.md#auth-architecture)).

### Error response format

All errors return JSON with a `detail` field:
```json
{ "detail": "Human-readable reason" }
```

Common status codes:

| Code | Meaning | Typical causes |
|---|---|---|
| 400 | Bad Request | validation failure, missing Turnstile token |
| 401 | Unauthorized | missing / invalid / expired access token, invalid refresh token |
| 403 | Forbidden | email not in `ALLOWED_EMAILS`, Turnstile verification failed, enhanced scan by a non-Premium user (`enhanced_requires_premium`) |
| 404 | Not Found | `user_book_id` / `book_id` doesn't exist or belongs to another user |
| 413 | Payload Too Large | request body > 10 MB (middleware), image > 5 MB (endpoint), or image pixel count above the cap (`image_too_large_pixels`) |
| 415 | Unsupported Media Type | file extension / MIME / magic bytes mismatch on upload, or an image that cannot be decoded (`image_undecodable`) |
| 402 | Payment Required | not returned by `/scan` since #582 (there is no monthly limit; older servers sent `scan_limit_reached` / `enhanced_limit_reached`) |
| 429 | Too Many Requests | rate limit hit, daily scan limit reached (`daily_scan_limit`), the user's `/scan` requests for the day used up (`scan_requests_daily_limit`, #586), another scan still running (`scan_in_progress`), enhanced scans throttled after repeated empty enhanced scans (`enhanced_throttled`). `Retry-After` is listed in `Access-Control-Expose-Headers`, so browser clients can read it on cross-origin responses |
| 502 | Bad Gateway | upstream API (Google Books, Open Library) failure |
| 503 | Service Unavailable | DB down (/health/db), scan service unavailable, scanning switched off or at daily capacity |

### Rate limits

Per user for a valid access token, otherwise per client IP (IPv6 per /64; Cloudflare `CF-Connecting-IP` when `TRUSTED_PROXY_MODE=cloudflare`, see [security.md](security.md#client-ip-and-origin-lockdown)). Limits are set via env (see `app/core/config.py`):

| Bucket | Default | Applied to |
|---|---|---|
| `rate_limit_auth` | 5/min | `/auth/google`, `/auth/refresh`, `/auth/logout` |
| `rate_limit_scan` | 10/min | `POST /scan` |
| `rate_limit_books_search` | 30/min | `GET /books/search` |
| `rate_limit_writes` | 60/min | `POST /wishlist`, `POST /wishlist/unverified`, `POST /purchased`, `PATCH` / `DELETE /user-books/*` |
| `rate_limit_reads` | 120/min | `GET /user-books`, `GET /auth/me`, `GET /scan/usage` |
| `rate_limit_health` | 60/min | `GET /health`, `GET /health/db` |

429 responses are logged at WARN level.

---

## Auth

### `POST /auth/google` — exchange Google ID token for Bookshelf tokens

Verifies the ID token against Google's JWKS, checks the email against `ALLOWED_EMAILS`, creates or updates the User row, issues fresh access + refresh tokens.

**Auth:** none
**Rate limit:** `rate_limit_auth`
**Source:** `backend/app/api/auth.py:google_auth`

**Request:**
```json
{ "id_token": "<google-id-token-jwt>" }
```

The ID token's `aud` claim must match one of `GOOGLE_CLIENT_ID`, `GOOGLE_IOS_CLIENT_ID`, or `GOOGLE_ANDROID_CLIENT_ID` — this is how the same API serves web + iOS + Android.

**Response `200`:**
```json
{
  "access_token": "eyJhbGciOiJSUzI1NiIs...",
  "refresh_token": "eyJhbGciOiJSUzI1NiIs...",
  "token_type": "bearer",
  "expires_in": 86400
}
```

**Errors:**
- `401` — token verification failed / email not verified
- `403` — email not in `ALLOWED_EMAILS`

**Example:**
```bash
curl -X POST https://bookshelfapi.buffingchi.com/auth/google \
  -H "Content-Type: application/json" \
  -d '{"id_token":"eyJhbGci..."}'
```

---

### `POST /auth/refresh` — rotate refresh token + issue new access token

Validates the refresh JWT, checks the `jti` exists + is not revoked, then **revokes the old `jti` and issues a new pair**. Refresh tokens are single-use.

**Auth:** none (the refresh token in the body is the credential)
**Rate limit:** `rate_limit_auth`
**Source:** `backend/app/api/auth.py:refresh`

**Request:**
```json
{ "refresh_token": "<refresh-jwt>" }
```

**Response `200`:** same shape as `/auth/google`.

**Errors:**
- `401` — token expired / invalid signature / wrong type / `jti` revoked or not found / user not found

**Example:**
```bash
curl -X POST https://bookshelfapi.buffingchi.com/auth/refresh \
  -H "Content-Type: application/json" \
  -d '{"refresh_token":"eyJhbGci..."}'
```

---

### `POST /auth/logout` — revoke refresh token

Revokes the `jti` embedded in the presented refresh token. Idempotent — unknown/already-revoked tokens return 204 (logout always "succeeds").

**Auth:** none
**Rate limit:** `rate_limit_auth`
**Source:** `backend/app/api/auth.py:logout`

**Request:**
```json
{ "refresh_token": "<refresh-jwt>" }
```

**Response `204`:** empty body.

**Example:**
```bash
curl -X POST https://bookshelfapi.buffingchi.com/auth/logout \
  -H "Content-Type: application/json" \
  -d '{"refresh_token":"eyJhbGci..."}'
```

---

### `GET /auth/me` — current user profile

**Auth:** required (Bearer access token)
**Rate limit:** `rate_limit_reads`
**Source:** `backend/app/api/auth.py:get_me`

**Response `200`:**
```json
{
  "id": "a9e4e6c2-...",
  "email": "you@example.com",
  "display_name": "You",
  "avatar_url": "https://lh3.googleusercontent.com/...",
  "created_at": "2026-03-15T12:00:00Z"
}
```

**Example:**
```bash
curl https://bookshelfapi.buffingchi.com/auth/me \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

---

## Books

### `GET /books/search` — free-text search

Queries Google Books, maps the top 3 volumes to candidates, then runs them through enrichment (Open Library work_id lookup, cover resolution).

**Auth:** required
**Rate limit:** `rate_limit_books_search`
**Source:** `backend/app/api/books.py:search_books`

**Query params:**
- `q` (required, min length 2) — free-text query, e.g. `"John Adams McCullough"`

**Response `200`:** array of up to 3 `EnrichedBook` objects (see shape under `/scan`). Each is checked against the requesting user's library like a scan result (`ownership`, `already_in_library`, `in_library`, #587); if that check fails the search still answers, with every result `not_owned`. Each carries a `catalog_token` signed for the requesting user (#602, see below).

**Errors:**
- `422` — `q` missing or too short
- `502` — Google Books unavailable

**Example:**
```bash
curl "https://bookshelfapi.buffingchi.com/books/search?q=dune+herbert" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

---

## Scan

### `POST /scan` — identify book cover from image

Multipart upload → optional Cloudflare Turnstile bot check → file validation (extension + MIME + size, read in chunks + magic bytes) → image normalisation (full decode, pixel cap, EXIF orientation, metadata stripped, long edge ≤ 1568 px, JPEG quality 85) → vision identification through the AI gateway (`gpt-4o-mini` by default; Claude when a feature is switched to `anthropic`, #532) → Google Books + Open Library enrichment → per-user deduplication flagging. Returns a `ScanResponse` whose `books` holds up to 15 `EnrichedBook` entries (`SCAN_MAX_BOOKS`), one per distinct book found in the photo.

**Auth:** required
**Rate limit:** `rate_limit_scan` (10/min default)
**Source:** `backend/app/api/scan.py:scan`

**Request:** `multipart/form-data`
- `file` (required) — image, max 5 MB, extensions `.jpg` / `.jpeg` / `.png` / `.webp` / `.heic` / `.heif`, `Content-Type` must start with `image/`, magic bytes must match the declared extension
- `cf-turnstile-response` (required iff `TURNSTILE_SECRET_KEY` is set on the server) — Turnstile token from the client widget
- `enhanced` (optional, default `false`) — `true` re-scans with the stronger model. Premium only (#533); see the rules below
- `attempt` (optional, integer 1–6, default `1`) — which try of this scan the request is (#603): `1` the first try, `2`–`6` the app's scheduled retries (#574). A missing or empty value means `1`. Anything else (not an integer, e.g. `x` or `1.5`, or out of range, e.g. `0` or `7`) is a `422` validation error like any other bad form field: it is rejected before the handler runs, so nothing is counted (not even the per-user daily request count, #586, or the 10/minute rate limit, which is applied by the handler's decorator), reserved or recorded. Parsing is Pydantic's lenient integer parsing, so `01`, `+3`, `1.0` and surrounding whitespace are accepted as integers. Purely informational: it is stored on every `ai_usage` row the request writes and in the `scan_record` log line, and never changes the quota, the charge, the cost, the model, the limits or the order of the checks

**Response `200`:** a `ScanResponse` object (`backend/app/schemas/book.py`):
- `books` — array of 0–15 `EnrichedBook` objects (at most `SCAN_MAX_BOOKS`), one per distinct book in the photo
- `enhanced` — `true` when the enhanced model was used
- `enhanced_scans_remaining` — integer or `null`: how many more enhanced scans the user can make today, i.e. the Premium found allowance left after this scan (`quota.premium.found_daily` minus today's found count; an enhanced scan uses the shared found unit, #582, #577); `null` when the user is not Premium (#533). Before #582 it was the enhanced monthly allowance left. Replaces the removed `enhanced_scan_credits` field
```json
{
  "books": [
    {
      "book_id": null,
      "open_library_work_id": "OL45804W",
      "google_books_id": "B1hSG45JCX4C",
      "title": "Dune",
      "author": "Frank Herbert",
      "description": "A mesmerizing space opera...",
      "cover_url": "https://books.google.com/...",
      "subjects": ["Science fiction", "Arrakis"],
      "confidence": 0.92,
      "already_in_library": false,
      "in_library": false,
      "ownership": "not_owned",
      "editions": [
        { "isbn_13": "9780441013593", "publisher": "Ace", "publish_year": 2005, "format": "paperback", "page_count": 688 }
      ],
      "catalog_token": "eyJleHAiOjE3OTQ4NzM2MDAsInYiOiJ2MSJ9.3q2-7w…"
    }
  ],
  "enhanced": false,
  "enhanced_scans_remaining": null,
  "repeat": false,
  "no_match": null
}
```

`book_id` is `null` when the work isn't yet in the `books` table — it's created on `POST /wishlist` or `POST /purchased`. `ownership` (#587) says whether the requesting user already has this book, by edition. The edition compared is `editions[0]`, the one `POST /wishlist` would add; its ISBN-13 (an ISBN-10 is converted to ISBN-13) is compared with the editions in the user's library:

| `ownership` | Meaning |
|---|---|
| `owned_same_edition` | an edition in the user's library has the same ISBN |
| `owned_other_edition` | the work is in the library (same `open_library_work_id` or `google_books_id`) and no entry of it holds this ISBN, but one holds an edition with another ISBN |
| `owned_edition_unknown` | the work is in the library but one side has no ISBN to compare (the result has no edition ISBN, or the library entry has no edition) |
| `not_owned` | not in the user's library |

Only the requesting user's library counts. It is checked on every response (new scans, repeats served from a scan record, `GET /books/search`) and never stored in a scan record. `already_in_library` and `in_library` (#585) are kept for older clients: both are `true` exactly when `ownership` is not `not_owned` (see [data-model.md](data-model.md#dedup-strategy--why-work_id-not-isbn)). `repeat` (#585) is `true` when the answer came from the user's saved scan record for this exact photo (no AI call, nothing charged; see "Scan records" below) and `false` otherwise. Both fields are additive: older clients can ignore them.

`catalog_token` (#602) is the server's signature over this book's catalog fields (`open_library_work_id`, `google_books_id`, `title`, `author`, `description`, `cover_url`, `subjects`, and per edition `isbn_13`, `isbn_10`, `publisher`, `publish_year`, `format`, `page_count`, `open_library_edition_id`) and the requesting user. It is opaque: the app keeps it unchanged and posts it back with the book, fields unchanged, to `POST /wishlist`. Every signed field comes from Open Library / Google Books: when a provider matched, `title` and `author` are the provider's (not the vision model's reading of the photo), and an edition `isbn_13` / `isbn_10` is present only when a provider confirmed it. A book with no provider ID (neither `open_library_work_id` nor `google_books_id`; nothing matched, so its title and author are the model's reading) always has `catalog_token: null` and cannot be added to the wishlist. Every response signs afresh (new scans, repeats served from a scan record, and `GET /books/search`); it is never stored in a scan record. It expires 30 days after the end of the UTC day it was issued, so the same result signed for the same user on the same day gives the same token. A book the server could not sign has `catalog_token: null` and can only be linked to an existing catalog entry.

`no_match` (#577) is set only for a **standard** scan whose provider answered properly but found no book (`"books": []`, a genuine no-match): `"free"` — it did not use a scan (it moved to the day's no-match allowance); `"counted"` — it used a scan, because today's no-match allowance is used up. It is `null` otherwise: books found, an enhanced scan, a repeat served from a scan record, or when the decision could not be made or confirmed (a lost database acknowledgement or a failed move: the scan then stays counted as a scan). Additive: older clients can ignore it.

**Errors:**
- `400` — Turnstile token missing (when configured)
- `403` — Turnstile verification failed
- `413` — image > 5 MB (`detail: "Image must be 5MB or smaller"`); the upload is read in chunks and reading stops as soon as it passes 5 MB
- `413` — the image's pixel count (width × height, read from its header before decoding) is above `SCAN_MAX_IMAGE_PIXELS` (default 40,000,000) (`detail: "image_too_large_pixels"`)
- `415` — bad extension / MIME / magic bytes
- `415` — the image cannot be fully decoded as JPEG, PNG, WebP or HEIC/HEIF (`detail: "image_undecodable"`)
- `422` — a form field fails validation (FastAPI's standard body, `detail[].loc` names the field), e.g. `attempt` not an integer or outside 1–6 (#603), or `enhanced` not a boolean. Sent before the handler runs (after authentication), so the request is not counted against `scan.requests_per_user_daily` (#586) and nothing is reserved or recorded
- `403` — `enhanced=true` from a user who is not Premium (`detail: "enhanced_requires_premium"`, #533). The tier is decided on the server (`entitlements.tier_for`); nothing is reserved and no provider is called
- No `402`. There is no monthly limit (#582), so `402 scan_limit_reached` (monthly allowance) and `402 enhanced_limit_reached` (Premium enhanced monthly allowance) are never sent. Clients should still treat an unexpected `402` from an older server as a generic failure.
- `429` — the user has made today's allowed number of `/scan` requests (`detail: "scan_requests_daily_limit"`, #586). Every authenticated request that gets past the kill switches counts, whatever happens next: a new photo, a repeat served from a scan record, a request refused later (Turnstile, a bad upload, the allowance, `scan_in_progress`, ...) or a provider failure; nothing is given back. The limit is the `ai_settings` key `scan.requests_per_user_daily` (default 100, 1..10,000). It is checked right after the kill switches, before Turnstile, the upload, the scan-record lookup, the allowance and the provider, and the refused request is not counted. `Retry-After` gives the seconds until the next UTC midnight, when the count resets. Distinct from the per-minute rate limit's `429` (10 a minute, slowapi's own body, no `detail` code) and from `daily_scan_limit` (scans that reached the AI). Repeats are free (#585), so this is what stops a client hammering `/scan` (decision #573 point 3)
- `429` — another scan by the same user is still running (`detail: "scan_in_progress"`, `Retry-After: 2`). One paid scan per user may be in flight at a time.
- `429` — the user's daily scan limit is reached (the **found** counter, #577; standard and enhanced scans count together; `detail: "daily_scan_limit"`). It is sent when found is full even if the no-match allowance has room: the next photo might find books. `Retry-After` gives the seconds until the next UTC midnight, when the count resets.
- `429` — enhanced scans are throttled for this user (`detail: "enhanced_throttled"`, #533): `scan.enhanced.zero_result_action` is `throttle` and the user already has `scan.enhanced.zero_result_threshold` empty enhanced scans recorded inside the last `scan.enhanced.zero_result_window_hours`. `Retry-After` gives the seconds until enough of them leave the window (at least 1; the whole window when the threshold is 0). Standard scans are not affected
- `503` — OpenAI unavailable (returns `detail: "scan_unavailable"`); the scan is not counted against the daily allowance. The same `503 scan_unavailable` is returned when the provider answered but its reply was malformed (#572: missing, not JSON, the wrong shape, a non-empty list in which every item is structurally broken, an unexpected stop reason, or cut off by the token limit before a single complete book): the user gets a retryable "try again", never "no books found", and the daily unit is refunded. A genuine "no books" answer is still a `200` with `"books": []` and uses the no-match allowance while it lasts (#577), else a scan
- `503` — scanning switched off by the operator (`detail: "scan_disabled"`, `ai_settings` key `scan.standard.enabled=false`); returned before the upload is read, Turnstile is called or any provider is contacted (the multipart body is still received by the framework)
- `503` — enhanced scanning switched off (`detail: "enhanced_scan_disabled"`, `ai_settings` key `scan.enhanced.enabled=false`); standard scans still work
- `503` — the server's AI settings cannot be read and none are cached (`detail: "ai_unavailable"`); returned before the upload is read
- `503` — the service's daily AI budget is reached and the user is on the Free tier (`detail: "ai_budget_reached"`, #531, owner decision on #531): Free scans pause until UTC midnight. The frontend shows it as "Scanning is paused". The ceiling is the `ai_settings` key `breaker.free_daily_ceiling_micros` (micro-dollars, seeded from `SCAN_FREE_DAILY_CEILING_MICROS`, default 10,000,000 = $10). Premium scans are not paused by it (decision #544); they are bound by their own daily allowance and the count cap below. Nothing is reserved for the user and no provider is called
- `503` — the service-wide daily scan capacity is used up (`detail: "scan_capacity"`, `ai_settings` key `scan.global_daily_limit`, seeded from `SCAN_GLOBAL_DAILY_LIMIT`, default 2000)

Allowances (`ai_settings`, by tier; everyone is Free until purchases exist; #577). Each user has two daily counters (UTC days):
- **found** (quota key `scan`): Free 5 a day (`quota.free.found_daily`), Premium 5 (`quota.premium.found_daily`). Every scan reserves a found unit before the provider is called; when it is full the scan is refused (`429 daily_scan_limit`). Standard and enhanced scans share it (OD-1): 3 standard + 2 enhanced scans use a found limit of 5.
- **no-match** (quota key `scan.no_match`): Free 5 a day (`quota.free.no_match_daily`), Premium 5 (`quota.premium.no_match_daily`). When a **standard** scan's provider answered properly but found no book, its found unit is given back and a no-match unit is taken instead (`"no_match": "free"`), so an unlucky photo does not use a scan. When no-match is full the found unit stays used (`"no_match": "counted"`).

So a user's provider calls per day never exceed found + no-match. Only real provider calls count: a repeat from a scan record, a malformed reply and a provider failure touch neither counter; a refusal by the model or a reply cut off with books found counts as found. Enhanced no-match scans never move to no-match (they keep the zero-result rule below). The service-wide `scan.global_daily_limit` counts both counters. The old keys `quota.free.daily` / `quota.premium.daily` are retired (the CLI rejects them; a row is ignored, except that an operator-set value is carried over to `found_daily` while no `found_daily` row exists).

**No monthly limit (#582).** There is no monthly allowance for any tier and no Premium enhanced monthly allowance; no scan is refused for a monthly reason. A month is bounded by each user's daily limit (at most 31 days of it), the service-wide `scan.global_daily_limit`, the daily spend ceiling (`breaker.free_daily_ceiling_micros`, so at most about 31 × the ceiling a month for Free) and, outside the service, the monthly spend limit set in the provider console (#542). Spend and call counts per month stay visible in the usage ledger (`ai_usage` / `ai_usage_daily`): tracking, not a limit.

Enhanced scans (#533) are Premium-only. An enhanced scan uses the shared daily unit and nothing else (#582; a per-model daily Sonnet counter comes with #583), so the daily allowance is what limits enhanced scans. When the provider answered but returned no books, the scan stays counted as a found scan (no move to no-match, #577) and is recorded (table `ai_enhanced_zero_results`) as long as the user has fewer than `scan.enhanced.zero_result_threshold` (default 3) such recorded scans in the last `scan.enhanced.zero_result_window_hours` (default 24, sliding); the count reaching the threshold sends one Sentry warning per user per window. At or above the threshold, `scan.enhanced.zero_result_action` decides: `charge` (default) records nothing more until the window clears; `throttle` refuses further enhanced scans (`429 enhanced_throttled`). Threshold 0: with `charge` nothing is recorded; with `throttle` every enhanced scan is refused. The daily unit is counted for every scan, empty or not. A provider error, timeout or malformed reply (#572) is never an empty scan: nothing is recorded. None of these settings can be read or changed by a client.

Check order: AI settings read (`ai_unavailable`) → kill switches → per-user daily request count (`scan_requests_daily_limit`, #586) → Turnstile → file validation (extension/MIME → capped read → magic bytes → ClamAV) → image normalisation → scan-record lookup (#585) → entitlement (tier; `enhanced_requires_premium` for a non-Premium enhanced scan) → global daily capacity (`scan_capacity`) → daily cost ceiling (`ai_budget_reached`, Free only) → in-flight lease (`scan_in_progress`) → zero-result throttle (`enhanced_throttled`, enhanced with action `throttle` only) → found allowance (`daily_scan_limit`) → vision provider → (a standard no-match) the move to the no-match allowance, in the settle. Every refusal gives back what was already taken (the cost estimate, the lease, the daily reservation) and happens before the provider is called. A scan counts toward the found allowance once it reaches the provider; a standard scan that finds no book moves to the no-match allowance while it has room (#577); it is refunded only when the provider is unavailable or its reply was malformed (`503 scan_unavailable`). A `413`/`415` from file validation or normalisation never uses a scan.

**Scan records (#585).** Every photo is fingerprinted on the server (the SHA-256 of the image after normalisation, so only the exact same file matches). For each user the server keeps a scan record per fingerprint: the AI's answer, the Open Library / Google Books result, and which of those steps finished. Records are kept `scan_records.retention_days` (default 30) and are not keyed on the prompt or model, so an old answer is still served after either changes. The lookup happens after normalisation and before any allowance:
- **The same photo, all steps done:** the saved result is returned with `"repeat": true`. No AI call, no Google Books / Open Library call, no daily allowance used, no lease (so no `scan_in_progress`), not counted by the global capacity or the cost ceiling; `enhanced_scans_remaining` and `GET /scan/usage` are unchanged. Repeats are not capped on their own: like every request they count toward the per-user daily request limit (`scan.requests_per_user_daily`, default 100, `429 scan_requests_daily_limit`, #586), and the per-minute rate limit still applies.
- **The same photo, AI answer saved but enrichment missing** (a lookup failed last time): only the enrichment is redone, with no AI call and no charge; `"repeat": true`.
- **A new photo:** the normal path (allowance, AI, enrichment); `"repeat": false`. The answer is saved when the AI returned outcome `ok`, with books or without: a genuine "no books" answer is saved too (#572), so re-uploading a "no book found" photo is a free repeat (`"repeat": true`, `"books": []`). Refusals, truncated or malformed replies, errors and timeouts are never saved, so re-uploading after one of those is a normal scan.

A standard scan is answered by any record of the photo. An enhanced scan (Premium) is answered only by a record an enhanced scan produced; otherwise it takes the normal path and its answer replaces the saved one. The only refusals a repeat can get are the ones before the lookup (settings, kill switches, Turnstile, file validation, normalisation) and `403 enhanced_requires_premium` for an enhanced repeat by a non-Premium user. Records are per user; no client field names a record, a fingerprint, a user or a step, and a scan-record failure never fails a scan (it takes the normal path). `in_library` (per book) is checked against the user's library on every scan, repeats included.

**Example:**
```bash
curl -X POST https://bookshelfapi.buffingchi.com/scan \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -F "file=@/path/to/cover.jpg"
```

### `GET /scan/usage` — the current user's scan allowances

Auth required (`401` without a valid access token). Rate limit: `rate_limit_reads`. Read only; takes no parameters (any sent are ignored).

**Response 200:**
```json
{
  "used_today": 2,
  "daily_limit": 5,
  "found_used_today": 2,
  "found_daily_limit": 5,
  "no_match_used_today": 1,
  "no_match_daily_limit": 5,
  "used_month": null,
  "monthly_limit": null,
  "period_end": null,
  "enhanced_used_month": null,
  "enhanced_monthly_limit": null
}
```

`found_used_today` counts the scans that used a scan in the current UTC day (books found, no-match scans counted because the no-match allowance was used up, and calls kept after an error), standard and enhanced together (quota key `scan`); `found_daily_limit` is the caller's tier found allowance (`quota.<tier>.found_daily`). `no_match_used_today` counts standard scans that found no book and did not use a scan (quota key `scan.no_match`); `no_match_daily_limit` is `quota.<tier>.no_match_daily` (#577).

`used_today` and `daily_limit` carry the found counter again: kept for one release for older app builds, then removed.

**Deprecated, always `null` (#582):** `used_month`, `monthly_limit`, `period_end`, `enhanced_used_month` and `enhanced_monthly_limit`. There is no monthly limit any more. They stay in the body for one release so an older app build still gets the keys it reads, and are removed after it. (An older build that requires them to be numbers hides its usage counter instead of crashing.) The app offers an enhanced re-scan from `enhanced_scans_remaining` on scan responses.

**Errors:** `401` (no or invalid token), `429` (rate limit), `503 ai_unavailable` (AI settings cannot be read and none are cached).

---

## User books

### `POST /wishlist` — add book to wishlist

Finds the `Book` by `open_library_work_id`, then `google_books_id`, and the `Edition` by the first edition's `isbn_13`; then inserts a `UserBook` with `status=wishlisted` and `wishlisted_at=now()`.

**A book the user already has is refused with `409` (#587).** A library holds one entry per title (`uq_user_books_user_book`), so this endpoint no longer returns the existing entry: when the user already has an entry for the `Book`, for the same `open_library_work_id` or `google_books_id` (so the 409 agrees with what the scan showed), or an entry whose edition has the same ISBN as the posted first edition (ISBN-10 converted to ISBN-13, whichever `Book` it belongs to), nothing is written (not even a new catalog row) and `detail` says how they have it, with the same values and rules as `ownership` on scan results: `owned_same_edition` (same ISBN, or the same `edition_id`), `owned_other_edition` or `owned_edition_unknown`. Only the requesting user's library counts. Of two adds of the same book at once, the one that loses on `uq_user_books_user_book` gets the same 409.

**Shared-catalog rule (#602).** `books` and `editions` are shared by every user, so they are written only from server-produced data. A missing `Book` / `Edition` is created only when `catalog_token` verifies: signed by this server for **this user**, not expired, current version (`v1`), and every catalog field in the body exactly as signed. Without a valid token (missing, malformed, expired, another user's, or any field changed) the request may only link the user to a `Book` that already exists (and to an existing `Edition` of that book); if no such `Book` exists it is refused with `422 catalog_token_invalid` and nothing is written. A book with neither `open_library_work_id` nor `google_books_id` is never added, token or not: `422 book_not_in_catalog`. A user book is never linked to an edition of a different book (it is then saved without an edition). Existing catalog rows are never changed by this endpoint, signed or not.

**Auth:** required
**Rate limit:** `rate_limit_writes`
**Source:** `backend/app/api/user_books.py:add_to_wishlist`

**Request:**
```json
{
  "open_library_work_id": "OL45804W",
  "google_books_id": "B1hSG45JCX4C",
  "title": "Dune",
  "author": "Frank Herbert",
  "description": "A mesmerizing space opera...",
  "cover_url": "https://...",
  "subjects": ["Science fiction"],
  "editions": [
    { "isbn_13": "9780441013593", "publisher": "Ace", "publish_year": 2005, "format": "paperback", "page_count": 688 }
  ],
  "catalog_token": "eyJleHAiOjE3OTQ4NzM2MDAsInYiOiJ2MSJ9.3q2-7w…"
}
```

The `EnrichedBook` returned by `/scan` or `/books/search`, posted back as received (extra fields such as `confidence` are ignored). `catalog_token` is optional in the schema (at most 256 characters) but required to create a catalog entry.

**Response `201`:** the new `UserBook` joined with its `Book` and (optional) `Edition`:
```json
{
  "id": "...",
  "status": "wishlisted",
  "wishlisted_at": "2026-04-05T10:30:00Z",
  "purchased_at": null, "started_at": null, "finished_at": null,
  "notes": null, "rating": null,
  "book": { "id": "...", "title": "Dune", "author": "Frank Herbert", "editions": [...], ... },
  "edition": null,
  "created_at": "...",
  "updated_at": "..."
}
```

**Errors:**
- `422` — validation failure (`detail` is a list)
- `422 catalog_token_invalid` — the book is not in the catalog yet and the body has no valid `catalog_token` (#602); nothing was written. The app shows its "couldn't save" message
- `422 book_not_in_catalog` — the book has no `open_library_work_id` and no `google_books_id` (no provider matched it), so it can never be in the catalog (#602); nothing was written. The app shows the same "couldn't save" message
- `409 owned_same_edition` / `409 owned_other_edition` / `409 owned_edition_unknown` — the user already has this book (#587, see above); nothing was written. The app treats it as "already in your library", not as a failure: no retry, no error report

---

### `POST /wishlist/unverified` — add a book the catalog can't find, as the user's own unverified entry

For a book neither Open Library nor Google Books finds (`422 book_not_in_catalog` above), #616. Creates a private `user_custom_books` row and a `user_books` row (`status=wishlisted`, `custom_book_id` set, no `book_id`, no edition) for the requesting user only. **It never reads or writes `books` or `editions`**: the entry is visible only to its creator, no other user's scan, search, wishlist add or library check can match it, and its ISBN is never written to the shared `editions.isbn_13` (see [Shared catalog trust model](security.md#shared-catalog-trust-model)). It also never counts as "already owned" for a scan or search result (even the creator's), so it never blocks or changes adding a real catalog book later.

Concurrent adds by one user are serialised (the user's `users` row is locked), so the cap and the duplicate check hold under concurrency.

**Auth:** required
**Rate limit:** `rate_limit_writes`
**Source:** `backend/app/api/user_books.py:add_unverified_to_wishlist`, `backend/app/services/unverified_books.py`

**Request** (no other field is accepted: provider IDs, `cover_url`, `catalog_token`, `user_id`, `status` and so on are refused with `422`):
```json
{ "title": "Pond Notes", "author": "A. Local", "isbn": "0-306-40615-2" }
```
- `title`, `author` — required, 1–500 characters after trimming, no control characters.
- `isbn` — optional, at most 20 characters; an ISBN-13 or ISBN-10 with a correct check digit, hyphens and spaces allowed. An ISBN-10 is converted to ISBN-13. Empty or blank means none.

**Response `201`:** a `UserBookRead` with `unverified: true`. `book` carries the entry's own `id`, `title` and `author` only (no provider IDs, cover, description or editions), `edition` is `null`, and `unverified_isbn_13` is the normalised ISBN (or `null`):
```json
{
  "id": "...",
  "status": "wishlisted",
  "unverified": true,
  "unverified_isbn_13": "9780306406157",
  "book": { "id": "...", "title": "Pond Notes", "author": "A. Local", "open_library_work_id": null, "google_books_id": null, "cover_url": null, "editions": [], ... },
  "edition": null,
  "wishlisted_at": "...", "created_at": "...", "updated_at": "..."
}
```

**Errors:**
- `422` — validation failure (`detail` is a list; `loc` names the field, e.g. `["body", "isbn"]`), or an unknown field
- `409 unverified_duplicate` — the user already has an unverified entry with the same title and author (casefolded, punctuation and spacing ignored) or the same ISBN; nothing was written. Other users' entries never count
- `409 unverified_limit_reached` — the user already holds `UNVERIFIED_BOOKS_PER_USER_MAX` (default 200) unverified entries; nothing was written. Deleting one (`DELETE /user-books/{id}`) frees a place

---

### `GET /user-books` — list all books in user's library

**Auth:** required
**Rate limit:** `rate_limit_reads`
**Source:** `backend/app/api/user_books.py:list_user_books`

**Query params:**
- `status` (optional) — filter by `wishlisted` / `purchased` / `reading` / `read`

**Response `200`:** array of `UserBookRead`, ordered by `created_at DESC`, with `Book` + `editions` + optional `Edition` eager-loaded. Every row has `unverified` (#616): `false` for a catalog book; `true` for the user's own unverified entry, which is shaped as in `POST /wishlist/unverified` (`book` holds only its `id`, `title` and `author`; `unverified_isbn_13`). `PATCH` and `DELETE /user-books/{id}` work on both kinds; deleting an unverified entry also deletes its `user_custom_books` row. `POST /purchased` takes only catalog books (an unverified entry's `book.id` is `404`); use `PATCH` to change an unverified entry's status.

**Example:**
```bash
curl "https://bookshelfapi.buffingchi.com/user-books?status=reading" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

---

### `POST /purchased` — promote to purchased (or create directly)

Transitions an existing `UserBook` to `status=purchased` with `purchased_at=now()`, OR creates a new `UserBook` in `purchased` status if the user didn't have one yet for this book. Optionally binds an edition via `edition_id` or by ISBN-13 lookup.

**Auth:** required
**Rate limit:** `rate_limit_writes`
**Source:** `backend/app/api/user_books.py:add_purchased`

**Request:**
```json
{
  "book_id": "<uuid>",
  "isbn_13": "9780441013593",
  "edition_id": null
}
```
Either `isbn_13` or `edition_id` may be provided (or neither). If `isbn_13` is provided and matches an existing `Edition` of the book, that edition is bound.

**Response `201`:** the `UserBook` (same shape as `/wishlist` response).

**Errors:**
- `404` — `book_id` not found

---

### `PATCH /user-books/{user_book_id}` — update status / notes / rating

Partial update. When `status` transitions, the corresponding timestamp column is set automatically if not already set (`purchased_at` / `started_at` / `finished_at`).

**Auth:** required
**Rate limit:** `rate_limit_writes`
**Source:** `backend/app/api/user_books.py:update_user_book`

**Request (any subset of fields):**
```json
{
  "status": "reading",
  "notes": "Deep first chapter",
  "rating": 5
}
```

**Validation:**
- `status`: one of `wishlisted` / `purchased` / `reading` / `read`
- `rating`: 1..5

**Response `200`:** the updated `UserBookRead`.

**Errors:**
- `404` — `user_book_id` not found or belongs to another user
- `422` — `rating` out of range

---

### `DELETE /user-books/{user_book_id}` — remove book from library

**Auth:** required
**Rate limit:** `rate_limit_writes`
**Source:** `backend/app/api/user_books.py:delete_user_book`

**Response `204`:** empty body.

**Errors:**
- `404` — not found or belongs to another user

---

## Health

### `GET /health` — liveness only

**Auth:** none — explicitly exempt from `TrustedHostMiddleware` so CI + uptime probes can hit the origin directly (see [architecture.md](architecture.md#adr-ci-health-checks-bypass-cloudflare))
**Rate limit:** `rate_limit_health`
**Source:** `backend/app/main.py:health`

Never touches the database — always `200`. Render restarts a service on a failed health check, so liveness must not depend on DB availability.

**Response `200`:**
```json
{ "status": "ok" }
```

### `GET /health/db` — DB connectivity check

**Auth:** none, but — unlike `/health` — NOT exempt from `TrustedHostMiddleware` or the origin lockdown, because it opens a DB connection. The DB-connectivity uptime monitor uses the public hostname anyway.
**Rate limit:** `rate_limit_health`
**Timeout:** the DB probe is bounded to 5s (`_HEALTH_DB_TIMEOUT_SECONDS`); a hang counts as failure
**Source:** `backend/app/main.py:health_db`

**Response `200`:**
```json
{ "status": "ok", "db": "ok" }
```

**Response `503`:** `{ "status": "error", "db": "error" }` when DB is unreachable or the probe times out.

---

## Common patterns

### Full token lifecycle

```
1. Client: POST /auth/google {id_token}
   → {access_token (24h), refresh_token (7d)}

2. Client: POST /auth/me with Authorization: Bearer <access>
   → 200

3. ... 24h later, access expires ...

4. Client: GET /user-books with expired access → 401

5. Client: POST /auth/refresh {refresh_token}
   → {access_token (new), refresh_token (new, old revoked)}

6. Client: retry GET /user-books with new access → 200

7. User logs out: POST /auth/logout {refresh_token}
   → 204 (refresh jti revoked)
```

### Making an authenticated request from curl

```bash
# Grab tokens once
TOKENS=$(curl -s -X POST $API/auth/google \
  -H "Content-Type: application/json" \
  -d "{\"id_token\":\"$GOOGLE_ID_TOKEN\"}")
ACCESS=$(echo "$TOKENS" | jq -r .access_token)

# Use for subsequent requests
curl "$API/user-books" -H "Authorization: Bearer $ACCESS"
```

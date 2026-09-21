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
| 403 | Forbidden | email not in `ALLOWED_EMAILS`, Turnstile verification failed |
| 404 | Not Found | `user_book_id` / `book_id` doesn't exist or belongs to another user |
| 413 | Payload Too Large | request body > 10 MB (middleware) or image > 5 MB (endpoint) |
| 415 | Unsupported Media Type | file extension / MIME / magic bytes mismatch on upload |
| 429 | Too Many Requests | rate limit hit |
| 502 | Bad Gateway | upstream API (Google Books, Open Library) failure |
| 503 | Service Unavailable | DB down (/health/db), scan service unavailable |

### Rate limits

Per-client-IP (Cloudflare `CF-Connecting-IP` in prod). Limits are set via env (see `app/core/config.py`):

| Bucket | Default | Applied to |
|---|---|---|
| `rate_limit_auth` | 5/min | `/auth/google`, `/auth/refresh`, `/auth/logout` |
| `rate_limit_scan` | 10/min | `POST /scan` |
| `rate_limit_books_search` | 30/min | `GET /books/search` |
| `rate_limit_writes` | 60/min | `POST /wishlist`, `POST /purchased`, `PATCH` / `DELETE /user-books/*` |
| `rate_limit_reads` | 120/min | `GET /user-books`, `GET /auth/me` |
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

**Response `200`:** array of up to 3 `EnrichedBook` objects (see shape under `/scan`).

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

Multipart upload → optional Cloudflare Turnstile bot check → file validation (extension + MIME + magic bytes) → ChatGPT Vision identification (`gpt-4o-mini`) → Google Books + Open Library enrichment → per-user deduplication flagging. Returns up to 3 `EnrichedBook` candidates.

**Auth:** required
**Rate limit:** `rate_limit_scan` (10/min default)
**Source:** `backend/app/api/scan.py:scan`

**Request:** `multipart/form-data`
- `file` (required) — image, max 5 MB, extensions `.jpg` / `.jpeg` / `.png` / `.webp` / `.heic` / `.heif`, `Content-Type` must start with `image/`, magic bytes must match the declared extension
- `cf-turnstile-response` (required iff `TURNSTILE_SECRET_KEY` is set on the server) — Turnstile token from the client widget

**Response `200`:** array of 0–3 `EnrichedBook` objects:
```json
[
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
    "editions": [
      { "isbn_13": "9780441013593", "publisher": "Ace", "publish_year": 2005, "format": "paperback", "page_count": 688 }
    ]
  }
]
```

`book_id` is `null` when the work isn't yet in the `books` table — it's created on `POST /wishlist` or `POST /purchased`. `already_in_library` is `true` when the user already has a `UserBook` for this work (dedup keyed on work_id / volume_id — see [data-model.md](data-model.md#dedup-strategy--why-work_id-not-isbn)).

**Errors:**
- `400` — Turnstile token missing (when configured)
- `403` — Turnstile verification failed
- `413` — image > 5 MB
- `415` — bad extension / MIME / magic bytes
- `503` — OpenAI unavailable (returns `detail: "scan_unavailable"`)

**Example:**
```bash
curl -X POST https://bookshelfapi.buffingchi.com/scan \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -F "file=@/path/to/cover.jpg"
```

---

## User books

### `POST /wishlist` — add book to wishlist

Creates the underlying `Book` row if it doesn't exist (dedup keyed on `open_library_work_id` / `google_books_id`), then inserts a `UserBook` with `status=wishlisted` and `wishlisted_at=now()`.

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
  ]
}
```

Typically built from an `EnrichedBook` returned by `/scan` or `/books/search`.

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
- `422` — validation failure
- uniqueness violation on `(user_id, book_id)` if the book is already in the user's library (dedup enforcement)

---

### `GET /user-books` — list all books in user's library

**Auth:** required
**Rate limit:** `rate_limit_reads`
**Source:** `backend/app/api/user_books.py:list_user_books`

**Query params:**
- `status` (optional) — filter by `wishlisted` / `purchased` / `reading` / `read`

**Response `200`:** array of `UserBookRead`, ordered by `created_at DESC`, with `Book` + `editions` + optional `Edition` eager-loaded.

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

**Auth:** none, but — unlike `/health` — NOT exempt from `TrustedHostMiddleware`. At the raw origin an attacker could otherwise rotate a spoofed `CF-Connecting-IP` per request to dodge the rate limit and exhaust the DB pool; the DB-connectivity uptime monitor uses the public hostname anyway.
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

# Security

See [~/.claude/standards/security.md](~/.claude/standards/security.md) for universal security standards.

**Related docs:** [`architecture.md`](architecture.md#middleware-stack) (middleware stack + rate limiting) · [`integrations.md`](integrations.md#cloudflare) (Turnstile + WAF) · [`api.md`](api.md#conventions) (error response format) · [`data-model.md`](data-model.md#refresh_tokens-backendappmodelsrefresh_tokenpy) (JTI revocation table)

## Security Controls

| Concern | Solution |
|---------|----------|
| JWT algorithm confusion | Pinned to RS256; all other algorithms including `none` rejected |
| JWT expiry | 15-minute access token (`JWT_ACCESS_TTL_MINUTES`, 1-60; the retired `JWT_EXPIRY_HOURS` is ignored with a boot warning), 7-day refresh token with JTI revocation |
| Token revocation | Every access and refresh token carries `tv` = `users.token_version`. `get_current_user` compares it with the user row it already loads on every request (no cache, no extra query); malformed or different → `401 token_revoked`. Release upgrade (#725): a token without `tv` (issued before #689) is a legacy token. A legacy access token gets `401 token_outdated`, so the app refreshes instead of signing out; a legacy refresh token is read as `tv = 0`, so it is accepted only while the user's `token_version` is 0, still goes through the `jti`, `revoked_at`, expiry and row-lock checks, and rotates into a `tv` pair (logged at INFO with the user id only). Any sign out of all devices ends it. The legacy path is temporary and is to be removed after 2026-12-15 (`REFRESH_TOKEN_EXPIRY_DAYS` + margin after the release reaches production; re-anchor the date to the actual production release date). Removal is tracked in #735 (`TODO(#735)` in `app/auth/jwt.py`). Residual risk: old app builds (pre-#689 client, no shared in-flight refresh) that fire several requests at once can each send `/auth/refresh` with the same single-use legacy token; the losers get a 401 and that client signs the user out. The same race already exists for those builds on ordinary token expiry, so it is not a regression, and refresh-token reuse is deliberately not allowed. `POST /auth/logout-all` (`app/auth/sessions.py:revoke_all_sessions`) bumps the version in SQL and revokes all the user's refresh rows; `/auth/refresh` also rejects a refresh token whose `tv` is stale and locks the user row (`FOR UPDATE`) while it rotates, which closes a refresh racing the bump without leaving an unrevoked row. A `tv` edited without re-signing fails the RS256 signature. Account deletion (#291) and refresh-reuse detection (#690) are to call the same helper. Tests: `tests/security/test_token_revocation.py`, `tests/integration/test_token_version_db.py` (#689) |
| Refresh token theft | Single-use rotation — every `/auth/refresh` revokes the old `jti` and issues a new one |
| Single-user lock | `ALLOWED_EMAILS` env var checked post-Google-auth |
| Account identity | Users are matched by the verified Google subject (`sub` → `users.google_id`), never by email (`app/auth/identity.py`, #688). An email change at Google keeps the same user and refreshes the stored email, unless another user already holds the new address (then the stored email is kept). A new subject whose verified email already belongs to another user is rejected with `409 account_conflict` — never merged; logs carry user ids only. Email comparisons (conflict lookups and `ALLOWED_EMAILS`) ignore case; emails are stored as Google reports them. A unique violation at commit (a concurrent sign-in) is rolled back and the lookup retried once. Gap: `users.email` is unique only case-sensitively, so two *concurrent* first sign-ins with case-variant emails can both succeed until a unique index on `lower(email)` exists (follow-up). A locked-out owner is recovered by an operator — see [`RENDER.md` → Resolving a sign-in `account_conflict`](RENDER.md#resolving-a-sign-in-account_conflict-409); the app shows a dedicated "contact support" message for this 409 |
| Dev test login | `POST /auth/test-login` is registered only when `ENVIRONMENT` is `development`/`test`; staging and production refuse to boot with `TEST_AUTH_SECRET` set. It matches users by the synthetic subject `test_<email>`, so an email held by a real Google account gets 409 |
| Rate limiting | `slowapi` — keyed by user (`user:<sub>`) only for a verified **access** token, otherwise by client IP (one bucket per IPv4 address / per IPv6 /64); 10 req/min on `/scan`, 5/min auth, 120/min reads, etc. Unauthenticated routes (including `/auth/*`) are always per-IP |
| Real client IP | One resolver, `app/core/client_ip.py`, set by `TRUSTED_PROXY_MODE` (`cloudflare` / `none`; default: `cloudflare` in hardened environments, `none` otherwise). Forwarding headers are trusted only in `cloudflare` mode, and only a single valid `CF-Connecting-IP`; `X-Forwarded-For` is never read. uvicorn runs with `--no-proxy-headers`. See [Client IP and origin lockdown](#client-ip-and-origin-lockdown) |
| Origin lockdown | `OriginAuthMiddleware` (`app/core/origin_auth.py`): when `ORIGIN_AUTH_SECRET` is set, requests without the matching origin header get `403 {"detail":"forbidden"}` before routing or rate limiting; only `/health` is exempt. Disabled while the secret is empty |
| Host header spoofing | `TrustedHostMiddleware` (hardened only — staging + production) rejects requests with Host header outside `TRUSTED_HOSTS`; only `/health` (not `/health/db`) is exempted via `_HealthExemptTrustedHost` so origin-direct liveness probes still work |
| Request size limit | `RequestSizeLimitMiddleware` (pure ASGI, #557) caps every request body: 6 MB on `POST /scan` (`MAX_SCAN_REQUEST_BYTES` = the 5 MB image limit + 1 MB multipart overhead), 10 MB elsewhere. A declared `Content-Length` over the cap → 413 without reading the body; otherwise the bytes are counted as they arrive, so a chunked body (no `Content-Length`) is cut off with 413 as soon as it passes the cap and no handler runs on it. A malformed `Content-Length` (non-numeric, negative, conflicting values) → 400 |
| DDoS / Bot protection | Cloudflare free tier (Bot Fight Mode, HTTP DDoS protection) |
| Bot protection on /scan | Cloudflare Turnstile — opt-in via `TURNSTILE_SECRET_KEY` env var. Request must carry `cf-turnstile-response`; verified against Cloudflare siteverify with 5s timeout |
| CORS | Explicit method/header allowlist; wildcard origins rejected at config validation; only `Retry-After` is exposed to scripts (`expose_headers`), for the scan 429s |
| Input validation | Pydantic on all endpoints; extension + MIME + size + magic bytes on uploads |
| Upload normalisation | Every `/scan` image is fully decoded, bomb-checked, stripped of metadata, downscaled and re-encoded as JPEG before any paid call. See [Upload pipeline](#upload-pipeline-post-scan) |
| SQL injection | SQLAlchemy ORM throughout; no raw SQL |
| Shared-catalog integrity | `books` / `editions` are written only from server-produced provider data, via server-signed results (`catalog_token`). See [Shared catalog trust model](#shared-catalog-trust-model) |
| HTTPS | Render TLS termination; HSTS header (hardened — staging + production); Cloudflare Full-strict TLS |
| Secret scanning | `gitleaks` pre-commit hook + GitHub Secret Scanning |
| Dependency CVEs | Dependabot + `pip-audit` + `npm audit` + Trivy (OSV+NVD) in CI. Trivy suppressions live in `.trivyignore`: each needs an `exp:` date and a justification, and only dev/build-time tooling may be suppressed, never shipped code (#629) |
| SAST | Bandit (Python) — HIGH/MEDIUM findings block CI |
| DAST | OWASP ZAP active scan (weekly + manual dispatch) |
| CI supply chain | All reusable workflow refs pinned to commit SHA |
| Web token storage | In-memory `Map` (never `localStorage`); native uses `SecureStore` |
| Security headers | CSP, HSTS, X-Frame-Options, X-Content-Type-Options, Permissions-Policy |
| Error disclosure | Exception messages stripped from HTTP error responses; logged server-side only |
| Error-report PII | Three layers in `app/core/sentry_context.py` (`sentry_privacy_options()`): (1) `include_local_variables=False` — frame locals are never sent, because auth frames hold ID/refresh tokens inside model reprs that no key-name list can see; (2) `before_send` / `before_send_transaction` / `before_breadcrumb` strip URL query strings (the Google Books key travels as `?key=`); (3) a recursive `EventScrubber` for request bodies, headers and extras — request token names plus every secret-looking `Settings` field, derived from `Settings.model_fields` so it cannot drift, plus the configured origin-lockdown header name (`ORIGIN_AUTH_HEADER`). Client-identifying request headers are **removed** from every event (case-insensitively, by `before_send`): `CF-Connecting-IP`, `CF-Connecting-IPv6`, `CF-Pseudo-IPv4`, `True-Client-IP`, `X-Client-IP`, `X-Forwarded-For`, `X-Real-IP`, `Forwarded`, and `CF-IPCountry` (approximate location). `CF-Ray` is harmless and kept. `request.env` address keys (`REMOTE_ADDR` etc.) and `user.ip_address` are removed too. `tests/security/test_sentry_client_ip.py` checks this against a real Sentry client (#565). `send_default_pii=False`. Deployed events carry `release=bookshelf-api@<RENDER_GIT_COMMIT>` and the `environment` tag |

## Shared catalog trust model

`books` and `editions` are shared by every user (and `editions.isbn_13` is globally unique, so the first writer of an ISBN fixes that edition for everyone). The rule (#602): **they are written only from data the server itself produced** (Open Library / Google Books lookups during `POST /scan` and `GET /books/search`). Client input lands only in the user's own rows (`user_books`).

- **What is signed comes from the providers.** A signed result can create a shared row, so no signed field may be under the user's control. The vision model's reading of a photo (which a doctored cover controls) is only a search key: whenever Open Library or Google Books matched, `EnrichmentService` takes the title and author from the provider record (Open Library `title` / `author_name[0]`, otherwise Google Books `volumeInfo.title` / `authors[0]`; `Unknown` when neither names an author), and every other catalog field (IDs, description, cover, subjects, publisher, year, page count) already comes from the records. A record without a usable title is not treated as a match. **The providers must agree.** When both matched, the Google Books volume is used only if it is the same book as the Open Library doc (`records_agree` in `enrichment.py`): they share an ISBN (Open Library `isbn` vs Google Books `industryIdentifiers`), or their titles agree (`titles_agree`, with Google Books' separate `subtitle` folded in: the same main title — casefolded, punctuation stripped, a leading "the" / "a" / "an" removed, whitespace collapsed — with any subtitle after `:`, `(`, `[`, ` - ` or a dash either identical or only an edition note such as "Deluxe Edition", "40th Anniversary" or "A Novel", so "Saga: Book One" and "Saga: Book Two" never agree) and normalised first-author surnames both match. Otherwise the volume is dropped entirely (no `google_books_id`, description, cover, publisher, year or page count from it) and only Open Library's fields are used, so one book's Google Books ID is never wired to another book's work; with no Open Library match, Google Books alone is used. **ISBNs.** The candidate's ISBN-13 is kept only when a record used for the result lists it among its identifiers (Open Library `isbn`, Google Books `industryIdentifiers`): a lookup by that ISBN returning a record is not enough, since Google Books `q=isbn:X` is a keyword search that can return a volume not listing X. The ISBN-10 is kept beside a confirmed ISBN-13 only when it is that ISBN-13's ISBN-10 form (978 prefix, recomputed check digit), because an Open Library work lists every edition's ISBNs and a listed ISBN-10 may be another edition's; without a confirmed ISBN-13, a listed ISBN-10 is kept on its own. An ISBN only the model read is dropped. The result shown to the user is exactly what would be stored. `GET /books/search` builds its candidates from Google Books volumes and goes through the same enrichment, so it signs only provider data too. What a token does *not* stop: the user choosing which real provider record a photo or query matches.
- **A book with no provider ID is never creatable.** With neither `open_library_work_id` nor `google_books_id`, nothing matched and the title and author are the model's reading: such a result is returned with `catalog_token: null`, and `POST /wishlist` refuses it (`422 book_not_in_catalog`) before any lookup, even with a token a server once signed for it, so a replayed token cannot create rows. The user can instead add it as their own private unverified entry (#616, below), which never enters the catalog.
- **Scan records from before this rule.** Saved books carry a format marker (`record_v`); a record saved without it (its title / ISBNs may be the model's reading) is treated as having no enrichment, so a repeat redoes the Open Library / Google Books lookups (no AI call, nothing charged) rather than signing the old values.
- **Signing.** Every `EnrichedBook` from `POST /scan` (new scans and repeats served from a scan record) and `GET /books/search` that has a provider ID carries `catalog_token` (`app/core/catalog_token.py`): an HMAC-SHA256 over the canonical JSON (sorted keys, no whitespace) of the catalog fields, the requesting user's ID, the expiry and the version. Format: `base64url({"exp": <unix s>, "v": "v1"}) "." base64url(mac)`, unpadded; decoding is strict (any character outside `[A-Za-z0-9_-]` makes the token malformed). Expiry: the end of the issuing UTC day plus 30 days (`CATALOG_TOKEN_TTL`, the scan-record retention, since results can wait in the app). Tokens are never stored in scan records.
- **Key.** `HMAC-SHA256(JWT_PRIVATE_KEY, "bookshelf-catalog-token-v1")`. `JWT_PRIVATE_KEY` is the one secret every instance shares (Render runs two), so any instance verifies any other's tokens. In a hardened environment (staging, production) without it the app refuses to boot (`_validate_config`), and signing / verifying refuse too; development and test use a fixed, clearly named dev-only key. Rotating `JWT_PRIVATE_KEY` invalidates outstanding catalog tokens: those books can then only be linked if already in the catalog. A new token version gets a new label.
- **Verification (`POST /wishlist`).** The MAC is recomputed over the received fields and the authenticated user and compared with `hmac.compare_digest`. Valid: a missing `Book` / `Edition` may be created from those fields. Anything else (missing, malformed, wrong version, expired, another user's, any field changed): the request may only link the user to an existing `Book` (by work ID or Google Books ID) and an existing `Edition` of that book (by ISBN-13); otherwise `422 catalog_token_invalid`, nothing written. A user book is never linked to an `Edition` of a different `Book`, signed or not (the user book is then saved without an edition). Existing catalog rows are never modified from a request, signed or not.
- **Idempotent creation.** Rows are matched by work ID / Google Books ID / ISBN-13 before any insert, and each insert runs in a SAVEPOINT: when two first-time adds of the same work race, the loser of the unique constraint on `books.open_library_work_id`, the partial unique index on `books.google_books_id` (`uq_books_google_books_id`, #613) or `editions.isbn_13` re-reads (by work ID, then volume ID) and links the winner's row instead of failing, so two simultaneous first adds of a book known only by its Google Books ID end with one row. Migration 0015 merged the duplicates created before the index. Lookups by it still tolerate duplicates as a harmless guard (#611): `WishlistService` links the oldest row (`ORDER BY created_at, id LIMIT 1`) and the library check (`DeduplicationService`) is an existence test.
- **Sentry.** `catalog_token` is on the Sentry scrubber denylist (`app/core/sentry_context.py`).
- **Other paths.** `POST /purchased` takes a `book_id` and optional `isbn_13` / `edition_id` and only references existing rows; `PATCH` / `DELETE /user-books/*` touch only the user's `user_books` row (and, for an unverified entry, its `user_custom_books` row). No other code constructs `Book` or `Edition`.
- **Unverified entries are private (#616).** A book no provider finds can be added by the user as an unverified entry (`POST /wishlist/unverified`). It is stored in `user_custom_books`, a per-user table **outside** the shared catalog, referenced only by the creator's own `user_books` row (`custom_book_id`; `book_id` is then NULL, `ck_user_books_one_book`). So the trust model above is unchanged: `books` / `editions` are still written only from provider data, and this path never reads or writes them. Guardrails:
  - *Fails safe by design.* Every catalog lookup and library check (`WishlistService._find_book`, `held_entries` for `DeduplicationService` and the `POST /wishlist` 409, which inner-joins `books`) queries the catalog tables, where unverified entries do not exist, so no filter can be forgotten: another user's scan, search, wishlist add or duplicate check can never match or link one. The creator's own entry does not count as "already owned" either, so it never blocks or changes adding the real catalog book.
  - *No shared ISBN.* The user's ISBN is kept in `user_custom_books.isbn_13` (not unique, never used for matching across users), never in `editions.isbn_13`, so it cannot squat the real edition.
  - *No provider IDs, no cover.* The request schema accepts only `title`, `author` and `isbn` (`extra="forbid"`); provider IDs, `cover_url`, `catalog_token`, a user ID or a status are refused with `422`.
  - *Bounded.* Title and author 1–500 characters (also DB check constraints), no control characters; ISBN at most 20 characters and a valid ISBN-13/ISBN-10 check digit; at most `UNVERIFIED_BOOKS_PER_USER_MAX` (default 200) entries per user (`409 unverified_limit_reached`), checked with the user's row locked; `@limiter.limit(settings.rate_limit_writes)`.
  - *Owner-only reads.* Entries are read only through `user_books` rows scoped to the session's user (`GET /user-books`, `PATCH` / `DELETE /user-books/{id}`); another user's `PATCH` / `DELETE` is `404`, and `POST /purchased` with an entry's ID is `404` (it looks only in `books`).
  - *Marked.* Every response says `unverified: true`, and the app shows an "Unverified" badge.
  - *Correction flags (#631).* The request may name the scan candidate the form was pre-filled from (`scan_reference`: a UUID `scan_record_id` and a `candidate_index` 0–999, `extra="forbid"`), never the pre-filled values and never the flags (extra fields are `422`). The server loads the record with the session's user in the same query (`scan_records.lookup_by_id`), so another user's record, like a missing or expired one or an index out of range, is ignored with nothing recorded and the same response, and the endpoint cannot be used to probe whether a record exists. The comparison runs on the server against the candidate it stored; `scan_corrections` holds only the three booleans and the record's feature, tier and model, never text, and `scan_corrections_daily` has no user. The response is built before the correction step, which runs in its own transaction and never fails the save. Tests: `tests/security/test_scan_corrections_authority.py`, `tests/integration/test_scan_corrections_db.py`.
  - Tests: `tests/security/test_unverified_books_authority.py` (no database), `tests/integration/test_unverified_books_db.py` (Postgres, including a real catalog book with the same ISBN added afterwards, the cap under concurrency and migration 0016 down/up), `tests/unit/test_unverified_books.py`.
- **Logging.** Only the reason a token failed (`missing`, `malformed`, `wrong_version`, `expired`, `mismatch`) is logged, never the key or a token.
- **Tests.** `tests/security/test_wishlist_catalog_authority.py` (`pytest -m security`, no database), `tests/security/test_catalog_provider_fields.py` (provider-sourced fields, provider agreement, ISBN rules; no database), `tests/unit/test_catalog_token.py`, `tests/unit/test_enrichment_agreement.py`, and on Postgres `tests/integration/test_wishlist_catalog_token_db.py`.

## Upload pipeline (`POST /scan`)

Every step runs before the enhanced entitlement check (Premium-only, #533) and the quota reservations, so a rejected image never uses a scan. Order:

1. Kill switches (`ai_settings` keys `scan.standard.enabled`, `scan.enhanced.enabled`; `503 ai_unavailable` if the table cannot be read and nothing is cached, or the cached value is older than `AI_SETTINGS_MAX_STALE_SECONDS`; an invalid `*.enabled` value counts as disabled) → Turnstile. No request field, header or query can set an `ai_settings` key; they change only through the CLI or the database (`tests/security/test_ai_settings_authority.py`).
1a. **Per-user daily request count** (#586): one more `/scan` request for the user's UTC day (`ai_quota` key `scan.requests`, an exact conditional upsert keyed on the session's user id); at `scan.requests_per_user_daily` (default 100) → `429 scan_requests_daily_limit` before Turnstile, the upload read or any other step. Every authenticated request that passes the kill switches counts, repeats and later refusals included, so repeat photos (free since #585) and malformed uploads cannot be sent without bound. Nothing in the request can change the count or the limit.
2. Extension and `Content-Type` → `415`.
3. **Streaming read with cap.** The handler reads the upload in 64 KB chunks and stops as soon as it has more than 5 MB (`MAX_IMAGE_BYTES`) → `413 "Image must be 5MB or smaller"`. The handler reads and holds at most 5 MB + 1 byte, but the framework has already received the whole multipart body before the handler runs (spooled to a temp file past 1 MB). Before that, `RequestSizeLimitMiddleware` caps the whole request body on this route at 6 MB (`MAX_SCAN_REQUEST_BYTES`: `MAX_IMAGE_BYTES` + 1 MB for multipart framing and the other form fields), whether or not the request declares a `Content-Length` (#557). A declared length over 6 MB is a `413 "Request body too large"` before any byte is read. A chunked body is counted as it arrives: once it passes 6 MB the middleware stops passing bytes on (the app sees a client disconnect, so form parsing fails and the handler never runs: no kill-switch read, request count, Turnstile call, quota reservation or provider call), drops whatever the app tries to answer and sends the 413 itself. So the framework buffers or spools at most about 6 MB per scan request, never the 100 MB Cloudflare allows. A malformed `Content-Length` (not a non-negative integer, or conflicting values) is a `400 "Invalid Content-Length header"`. There is no slow-body timeout in the middleware.
4. **Magic bytes** (`app/core/file_validation.py`) must match the declared extension → `415`. This runs before anything decodes the bytes.
5. **ClamAV** (optional, `CLAMAV_ENABLED`) on the original bytes. Fails closed (#622): only an explicit `OK` result passes; `FOUND` → `422`; daemon unreachable, `ERROR`, an unrecognised status or a missing/malformed result → `503 "Malware scanning unavailable"`.
6. **Normalisation** (`app/ai/images.py`, called through `sanitize_image` in `app/core/file_security.py`):
   - Only the JPEG, PNG, WebP and HEIF decoders are tried (`pillow-heif` is registered for HEIC/HEIF at import). Other Pillow plugins (TIFF, GIF, EPS, PDF, ...) are unreachable from an upload.
   - **Decompression-bomb guard:** the pixel count declared in the header is checked before any pixel data is decoded. Above `scan.max_image_pixels` in `ai_settings` (seeded from `SCAN_MAX_IMAGE_PIXELS`; default 40 MP, range 1–100 MP) → `413 image_too_large_pixels`. Pillow's own `DecompressionBombError` maps to the same response.
   - **Decode or reject:** the image is fully decoded; anything Pillow cannot decode (truncated, corrupt, wrong format behind valid magic bytes) → `415 image_undecodable`. There is no raw-bytes fallback: undecodable bytes never reach the provider.
   - EXIF orientation is applied, then a fresh image is built so all metadata (EXIF including GPS, XMP, IPTC, ICC profiles, comments) is dropped. Re-encoding also removes polyglot payloads (scripts in comment segments, archives after the end-of-image marker).
   - It runs in a worker thread, at most `SCAN_NORMALISE_CONCURRENCY` (default 1, range 1–8) at once per process, so a burst of large images cannot block the event loop or multiply peak memory.
   - The long edge is shrunk to the feature's `max_image_long_edge_px` (registry, 1568 px for `scan.standard` and `scan.enhanced`); smaller images are never upscaled. Shrinking happens before alpha is flattened onto white; every mode is converted to RGB, with 16-bit greyscale scaled to 8-bit rather than clipped.
   - Output is always JPEG, quality 85. Only these bytes are sent to the provider, which caps the input cost per image (about 3,000 tokens at 1568 px).
   - The log line carries numbers only (output width × height and byte counts), never the image, a filename or any metadata value.
7. **Scan records** (`app/ai/scan_records.py`, #585), before the quota reservations. Records are per user: a record is found by the session's `user_id` and the SHA-256 of the normalised bytes, both decided by the server. No request field, header or query names a record, a fingerprint, a user, a tier, a model or a step, and one user can never be served another user's record for the same photo (`tests/security/test_scan_records_authority.py`). An enhanced repeat is refused for a non-Premium user (`403 enhanced_requires_premium`) exactly like a new photo, and an enhanced scan is never answered by a standard record. Repeats are free and uncapped (decision #573); the per-minute rate limit and Turnstile still apply to them, and the per-user daily `/scan` request limit is #586. **What is stored about a photo:** only its SHA-256 (of the normalised JPEG), the validated candidate list (title, author, confidence, ISBNs) and the enrichment result (public book metadata from Open Library / Google Books), at most `SCAN_MAX_BOOKS` each and only when at least one book was found, with the feature, tier and model; per user, for at most `scan_records.retention_days` (30); never the image bytes and never the model's raw output. Rows go with the account (cascade). The observability log line carries counts only (no user id, no content). The record's id is returned to its owner as `scan_record_id` (#631) and accepted back only on `POST /wishlist/unverified`, where it is looked up together with the session's user.

Library pins: `Pillow` and `pillow-heif` are pinned exactly in `backend/requirements.txt`.

- **Bundled native decoders:** the `pillow-heif` wheel bundles native libheif (1.23.4 in `pillow-heif` 1.8.0) and libde265 (1.1.3). CVEs in those C libraries are not visible to `pip-audit` or Trivy through PyPI advisories, so when updating `pillow-heif`, read each release's changelog for libheif/libde265 bumps (and check their upstream advisories) rather than relying on CI to flag them.
- **Licences:** the `pillow-heif` wheel bundles libheif/libde265 (LGPL-3.0) and x265 (GPL-2.0+); they are used server-side only and not distributed.

## Client IP and origin lockdown

Per-IP rate limits are only as good as the client IP they key on, and the API
can only trust a forwarding header on requests that really came through the
Cloudflare edge. Two controls work together.

**Client-IP resolver** (`app/core/client_ip.py`) is the only code that decides
a request's client IP. It is configured by `TRUSTED_PROXY_MODE`:

| Value | Client IP |
| --- | --- |
| `none` | The socket peer. All forwarding headers are ignored. |
| `cloudflare` | `CF-Connecting-IP` when the request has exactly one such header holding one valid IP address; otherwise the socket peer. `X-Forwarded-For` is never read. |
| unset or empty (default) | `cloudflare` in hardened environments (`staging`, `production`), `none` otherwise. |

Any other value fails settings validation, so the API refuses to boot rather
than trust the wrong header. Addresses are canonicalised with `ipaddress`
(IPv4-mapped IPv6 becomes IPv4), and rate-limit keys group IPv6 by /64.
uvicorn is started with `--no-proxy-headers` so the socket peer is never
rewritten from `X-Forwarded-For` before the resolver sees it.

**Origin lockdown** (`app/core/origin_auth.py`) uses the settings
`ORIGIN_AUTH_HEADER` (default `X-Origin-Auth`) and `ORIGIN_AUTH_SECRET`
(default empty, which disables it). A non-empty secret must be at least 32
printable ASCII characters with no leading or trailing whitespace, or the API
refuses to boot (the error names the rule, never the value); generate one with
e.g. `openssl rand -hex 32`. When the secret is set, the edge must add
that header with that value to every request it forwards. A request without
exactly one matching value (compared in constant time) gets
`403 {"detail":"forbidden"}` before routing, so no handler, DB query or
rate-limit counter runs. `/health` is the only exempt path, because Render's
health check calls the origin directly. The 403 carries the normal security
headers. The value never appears in responses or logs, and the header is
scrubbed from error reports by the Sentry scrubber (`build_event_scrubber()`
in `app/core/sentry_context.py` denylists the configured header name).
The secret is a dashboard-only Render secret (`sync: false`) and is never
committed.

**Rollout order:**

1. Optionally confirm `TRUSTED_PROXY_MODE` on each API service (not required:
   the default is already `cloudflare` in hardened environments). Update the
   start command to the one in `render.yaml`, which adds `--no-proxy-headers`
   (defence in depth).
2. Deploy, with `ORIGIN_AUTH_SECRET` unset.
3. The owner configures the edge to add the origin header to every request it
   forwards to the API hostname, and checks that it arrives.
4. Set `ORIGIN_AUTH_SECRET` on the Render service to the same value. From then
   on only requests that came through the edge (plus `/health`) are served,
   which is what makes `CF-Connecting-IP` trustworthy.

To roll back the lockdown, clear `ORIGIN_AUTH_SECRET`. To stop trusting
`CF-Connecting-IP`, set `TRUSTED_PROXY_MODE=none`.

## Logging

Application logs are structured JSON on stdout (`backend/app/core/logging.py`).

**Logged:** request IDs, internal user IDs (when known), HTTP status codes, counts, and for AI-model parse failures only the response length, a 12-character SHA-256 prefix and the finish reason.

**Never logged:** email addresses, Google client IDs / allowed audiences, the email allowlist, ID/access/refresh tokens, `Authorization` headers, or raw model output.

**Safety net:** a `RedactingFilter` replaces email addresses, `Bearer <token>` values, JWT-shaped strings (three base64url segments) and credential-looking URL query parameters (`key=`, `token=`, `api_key=`, …) with `[REDACTED*]` markers. It renders the full message once, so values nested in lists, dicts or objects are covered, and it also redacts exception tracebacks and stack info. The JSON formatter applies the same redaction to the message and traceback as a second layer. It uses precompiled regexes and never raises.

**Scope:** the filter is attached to the root handler (all `app.*` loggers) and to the `uvicorn`, `uvicorn.error` and `uvicorn.access` handlers, which don't propagate to root. On those handlers it runs in `preserve_args` mode: string args are redacted in place so uvicorn's access formatter, which unpacks the args, keeps working. Sentry events are scrubbed separately (`app/core/sentry_context.py`). It is a backstop, not a licence to log such values: new log lines should not include them in the first place.

## Claude API policy checks

`.claude/policies/claude.md` is enforced in CI by the `claude-policy` job, which runs `scripts/policy/check_claude_policy.py` over the tracked source tree (files under `app/ai/` or that import the Anthropic SDK; tests, docs and `.env*` are skipped). Fixture tests: `backend/tests/unit/test_claude_policy.py`. Checks:

1. No hardcoded `sk-ant-` key.
2. (AST) Files with an SDK call retry 429, 500, 503 and 529 and respect `retry-after`.
3. Model IDs are in `ALLOWED_MODELS["anthropic"]` (today `claude-haiku-5-5`, `claude-sonnet-5-5`, which are fixed IDs with no dated snapshot) or are dated snapshots. `*-latest` aliases and unlisted IDs fail.
4. `max_tokens` on every `messages.create` / `messages.stream` call.
5. `stop_reason` and `refusal` are handled.
6. SDK calls (`messages.create`, `AsyncAnthropic`, `Anthropic(`) only under `backend/app/ai/providers/`.
7. No raw content fields (text, content, output, image bytes) in `app/ai/` log calls; log a length or hash.
8. The breaker's refusal is `503 ai_budget_reached`.

## Pre-commit Hook (gitleaks)

Install: `brew install gitleaks` (or via pip on Windows)

```bash
# .git/hooks/pre-commit
gitleaks protect --staged --redact
```

---

## Penetration Testing Runbook

### Authorization

This is a personal application. The authorized tester is the repository owner.
All testing must be performed against:
- Staging/dev deployment **or** a dedicated test account on production
- Never against real user data

Create a dedicated test account: sign in with a Google account used only for testing.

### Tools

```bash
pip install jwt_tool        # JWT algorithm confusion, tampering
pip install pip-audit       # Python dependency CVE scan
npm install -g npm-check    # Dependency freshness
# Burp Suite Community (manual HTTP manipulation)
# ffuf (endpoint fuzzing)
```

### Cadence

| Frequency | What |
|-----------|------|
| Every CI run | Bandit, pip-audit, npm audit, Trivy |
| Weekly (automated) | OWASP ZAP active scan (`.github/workflows/security.yml`) |
| Quarterly (manual) | Full OWASP Top 10 checklist below |
| Every dep bump | `pip-audit -r backend/requirements.txt` locally before merging |

---

### OWASP Top 10 Manual Checklist

Run quarterly. Check each item off and record date + tester.

**A01 — Broken Access Control**
- [ ] Access user A's `user_book_id` as user B → expect 404 (ownership scoped via `_user_book_query(current_user.id)`)
- [ ] UUID enumeration on `GET /user-books/{id}` → always 404, never 403
- [ ] Confirm `GET /auth/me` returns only the current user — no `user_id` override accepted

**A02 — Cryptographic Failures**
- [ ] HSTS header present: `curl -I https://bookshelfapi.buffingchi.com/health`
- [ ] Token algorithm: `base64 -d <<< <header_b64>` → must show `"alg":"RS256"`
- [ ] Refresh token used as access token to `GET /auth/me` → 401

**A03 — Injection**
- [ ] `GET /books/search?q=' OR 1=1--` → 200 with normal results or 422, never 500 DB error
- [ ] Notes field XSS: PATCH `notes: "<script>alert(1)</script>"` → stored as plain text, React Native never renders as HTML
- [ ] Scan filename path traversal: `../../../../etc/passwd.jpg` → `rsplit(".", 1)[-1]` yields `"jpg"`, passes; magic bytes check validates content

**A05 — Security Misconfiguration**
- [ ] `/docs` returns 404 in production
- [ ] All required headers present: `curl -I https://bookshelfapi.buffingchi.com/health | grep -E "x-frame|csp|hsts|permissions"`
- [ ] CORS rejects `Origin: https://evil.example.com`

**A07 — Identification and Authentication Failures**
See "Auth Bypass Test Cases" section below.

**A08 — Software and Data Integrity**
- [ ] CI workflow refs use commit SHAs: `grep -r "@main" .github/workflows/` → empty
- [ ] Magic bytes check catches polyglot: submit JPEG magic + PHP body → forwarded to OpenAI (bytes never executed server-side; acceptable)

**A10 — SSRF**
- [ ] All outbound URLs are hardcoded constants (Google Books, Open Library, JWKS, OpenAI, Turnstile) — no user-supplied URL parameter accepted anywhere

---

### Auth Bypass Test Cases

Install: `pip install jwt_tool`

```bash
# 1. Algorithm confusion: RS256 → HS256 using public key as HMAC secret
jwt_tool <valid_access_token> -X a -pk public_key.pem
# Submit to GET /auth/me → expect 401

# 2. None algorithm
jwt_tool <valid_access_token> -X n
# Submit to GET /auth/me → expect 401

# 3. JTI replay: use same refresh token twice
# First call rotates the token; second call must return 401
curl -X POST /auth/refresh -d '{"refresh_token":"<old_rt>"}' → 200
curl -X POST /auth/refresh -d '{"refresh_token":"<old_rt>"}' → 401

# 4. Expired token
jwt_tool <valid_access_token> -T   # tamper exp to past
# Submit to GET /auth/me → expect 401

# 5. Token type confusion: submit refresh token to access-only endpoint
# Extract refresh token from storage, submit as Authorization: Bearer to GET /auth/me → 401
```

---

### Rate Limit Bypass Test Cases

```python
import requests, time

BASE = "https://bookshelfapi.buffingchi.com"
TOKEN = "<your_access_token>"

# Authenticated exhaustion — rate_limit_reads = 120/minute
# expect 429 on request 121
for i in range(130):
    r = requests.get(f"{BASE}/user-books", headers={"Authorization": f"Bearer {TOKEN}"})
    print(i, r.status_code)
    if r.status_code == 429:
        print("Rate limit hit at request", i)
        break

# Auth burst — rate_limit_auth = 5/minute
# expect 429 on request 6
for i in range(10):
    r = requests.post(f"{BASE}/auth/google", json={"id_token": "bad"})
    print(i, r.status_code)
```

**Known limitation:** Rate limits use in-memory storage (no Redis). Limits reset on uvicorn process restart. Acceptable for current personal-app scale.

**Expected:** a refresh token sent as `Authorization: Bearer` does not get a per-user bucket; it is limited by client IP like an anonymous request. Forwarding headers sent by a client (`X-Forwarded-For`, or `CF-Connecting-IP` when `TRUSTED_PROXY_MODE=none`) do not change which bucket a request lands in.

---

### File Upload Fuzzing (Burp Repeater)

| Test | Expected result |
|------|----------------|
| JPEG magic + PHP body (polyglot) | Passes magic bytes; fails full decode → `415 image_undecodable` (never forwarded) |
| Valid JPEG + `<script>` comment / zip after EOI | Re-encoded; payload absent from the bytes sent to the provider |
| PNG header declaring 9000×9000 px (tiny file) | `413 image_too_large_pixels`, no full decode |
| `Content-Type: image/gif` with valid JPEG body | Passes MIME check, fails magic bytes → 415 |
| `Content-Type: application/octet-stream` | Fails MIME check immediately → 415 |
| Filename `../../../../etc/passwd.jpg` | Extension `"jpg"` extracted via `rsplit(".", 1)[-1]`; magic bytes validated |
| No extension: `filename: "file"` | `"." in filename` is False → 415 |
| File exactly 5 MB | Should pass |
| File 5 MB + 1 byte | 413 (handler stops reading at 5 MB + 1 byte) |
| `POST /scan` body > 6 MB, chunked (`Transfer-Encoding: chunked`, no `Content-Length`) | `413 "Request body too large"` from the middleware once 6 MB has arrived; handler, quota and provider never run |
| `POST /scan` with `Content-Length` > 6 MB | `413 "Request body too large"` before any body byte is read |
| Any other route, chunked body > 10 MB | `413 "Request body too large"` |
| `Content-Length: abc`, `-1`, `+5`, empty, two different `Content-Length` headers or `1, 2` | `400 "Invalid Content-Length header"` (not 500) |

---

### ZAP Baseline Promotion Checklist

To promote `fail_action: false` → `fail_action: true` in `.github/workflows/security.yml`:

1. Run scan via `workflow_dispatch` on `security.yml`
2. Download `zap-full-scan-report` artifact from GitHub Actions
3. For each finding:
   - **True positive, fixable** → fix it, re-run
   - **True positive, accepted risk** → add `WARN` rule to `.zap/rules.tsv` with justification
   - **False positive** → add `IGNORE` rule to `.zap/rules.tsv` with explanation
4. When a clean run shows zero `FAIL`-level findings:
   - Change `fail_action: false` → `fail_action: true`
   - Change `cmd_options: '-z "-config scanner.attackStrength=LOW"'` → `MEDIUM`

---

### Incident Response Quick Reference

**Contain auth abuse immediately:**
```bash
# Activate email allowlist in Render env vars (blocks all non-listed sign-ins)
# ALLOWED_EMAILS=your@email.com
# Triggers re-deploy (~1-2 min)
```

**Revoke a specific user's sessions:**
```sql
UPDATE refresh_tokens
SET revoked_at = NOW()
WHERE user_id = '<uuid>' AND revoked_at IS NULL;
```

**Contain scan abuse:**
```bash
# Lower RATE_LIMIT_SCAN in Render env vars (e.g., "2/minute")
# OR set TURNSTILE_SECRET_KEY to activate bot challenge
```

**Post-incident:**
1. Export Render logs for the incident window
2. Check `users`, `user_books`, `refresh_tokens` for unexpected records
3. Run ZAP scan via `workflow_dispatch` post-fix
4. Update this doc if the incident reveals a new threat model

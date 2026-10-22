# Security

See [~/.claude/standards/security.md](~/.claude/standards/security.md) for universal security standards.

**Related docs:** [`architecture.md`](architecture.md#middleware-stack) (middleware stack + rate limiting) · [`integrations.md`](integrations.md#cloudflare) (Turnstile + WAF) · [`api.md`](api.md#conventions) (error response format) · [`data-model.md`](data-model.md#refresh_tokens-backendappmodelsrefresh_tokenpy) (JTI revocation table)

## Security Controls

| Concern | Solution |
|---------|----------|
| JWT algorithm confusion | Pinned to RS256; all other algorithms including `none` rejected |
| JWT expiry | 24hr access token, 7-day refresh token with JTI revocation |
| Refresh token theft | Single-use rotation — every `/auth/refresh` revokes the old `jti` and issues a new one |
| Single-user lock | `ALLOWED_EMAILS` env var checked post-Google-auth |
| Rate limiting | `slowapi` — keyed by user (`user:<sub>`) only for a verified **access** token, otherwise by client IP (one bucket per IPv4 address / per IPv6 /64); 10 req/min on `/scan`, 5/min auth, 120/min reads, etc. Unauthenticated routes (including `/auth/*`) are always per-IP |
| Real client IP | One resolver, `app/core/client_ip.py`, set by `TRUSTED_PROXY_MODE` (`cloudflare` / `none`; default: `cloudflare` in hardened environments, `none` otherwise). Forwarding headers are trusted only in `cloudflare` mode, and only a single valid `CF-Connecting-IP`; `X-Forwarded-For` is never read. uvicorn runs with `--no-proxy-headers`. See [Client IP and origin lockdown](#client-ip-and-origin-lockdown) |
| Origin lockdown | `OriginAuthMiddleware` (`app/core/origin_auth.py`): when `ORIGIN_AUTH_SECRET` is set, requests without the matching origin header get `403 {"detail":"forbidden"}` before routing or rate limiting; only `/health` is exempt. Disabled while the secret is empty |
| Host header spoofing | `TrustedHostMiddleware` (hardened only — staging + production) rejects requests with Host header outside `TRUSTED_HOSTS`; only `/health` (not `/health/db`) is exempted via `_HealthExemptTrustedHost` so origin-direct liveness probes still work |
| Request size limit | `RequestSizeLimitMiddleware` drops bodies > 10 MB with 413 before reading the stream |
| DDoS / Bot protection | Cloudflare free tier (Bot Fight Mode, HTTP DDoS protection) |
| Bot protection on /scan | Cloudflare Turnstile — opt-in via `TURNSTILE_SECRET_KEY` env var. Request must carry `cf-turnstile-response`; verified against Cloudflare siteverify with 5s timeout |
| CORS | Explicit method/header allowlist; wildcard origins rejected at config validation; only `Retry-After` is exposed to scripts (`expose_headers`), for the scan 429s |
| Input validation | Pydantic on all endpoints; extension + MIME + size + magic bytes on uploads |
| Upload normalisation | Every `/scan` image is fully decoded, bomb-checked, stripped of metadata, downscaled and re-encoded as JPEG before any paid call. See [Upload pipeline](#upload-pipeline-post-scan) |
| SQL injection | SQLAlchemy ORM throughout; no raw SQL |
| HTTPS | Render TLS termination; HSTS header (hardened — staging + production); Cloudflare Full-strict TLS |
| Secret scanning | `gitleaks` pre-commit hook + GitHub Secret Scanning |
| Dependency CVEs | Dependabot + `pip-audit` + `npm audit` + Trivy (OSV+NVD) in CI |
| SAST | Bandit (Python) — HIGH/MEDIUM findings block CI |
| DAST | OWASP ZAP active scan (weekly + manual dispatch) |
| CI supply chain | All reusable workflow refs pinned to commit SHA |
| Web token storage | In-memory `Map` (never `localStorage`); native uses `SecureStore` |
| Security headers | CSP, HSTS, X-Frame-Options, X-Content-Type-Options, Permissions-Policy |
| Error disclosure | Exception messages stripped from HTTP error responses; logged server-side only |
| Error-report PII | Three layers in `app/core/sentry_context.py` (`sentry_privacy_options()`): (1) `include_local_variables=False` — frame locals are never sent, because auth frames hold ID/refresh tokens inside model reprs that no key-name list can see; (2) `before_send` / `before_send_transaction` / `before_breadcrumb` strip URL query strings (the Google Books key travels as `?key=`); (3) a recursive `EventScrubber` for request bodies, headers and extras — request token names plus every secret-looking `Settings` field, derived from `Settings.model_fields` so it cannot drift, plus the configured origin-lockdown header name (`ORIGIN_AUTH_HEADER`). `send_default_pii=False`. Deployed events carry `release=bookshelf-api@<RENDER_GIT_COMMIT>` and the `environment` tag |

## Upload pipeline (`POST /scan`)

Every step runs before the enhanced entitlement check (Premium-only, #533) and the quota reservations, so a rejected image never uses a scan. Order:

1. Kill switches (`ai_settings` keys `scan.standard.enabled`, `scan.enhanced.enabled`; `503 ai_unavailable` if the table cannot be read and nothing is cached, or the cached value is older than `AI_SETTINGS_MAX_STALE_SECONDS`; an invalid `*.enabled` value counts as disabled) → Turnstile. No request field, header or query can set an `ai_settings` key; they change only through the CLI or the database (`tests/security/test_ai_settings_authority.py`).
2. Extension and `Content-Type` → `415`.
3. **Streaming read with cap.** The handler reads the upload in 64 KB chunks and stops as soon as it has more than 5 MB (`MAX_IMAGE_BYTES`) → `413 "Image must be 5MB or smaller"`. The handler reads and holds at most 5 MB + 1 byte, but the framework has already received the whole multipart body before the handler runs (spooled to a temp file past 1 MB). `RequestSizeLimitMiddleware` rejects a request whose `Content-Length` is above 10 MB, but it checks only that header, so a chunked body (no `Content-Length`) is not capped yet — see #557.
4. **Magic bytes** (`app/core/file_validation.py`) must match the declared extension → `415`. This runs before anything decodes the bytes.
5. **ClamAV** (optional, `CLAMAV_ENABLED`) on the original bytes.
6. **Normalisation** (`app/ai/images.py`, called through `sanitize_image` in `app/core/file_security.py`):
   - Only the JPEG, PNG, WebP and HEIF decoders are tried (`pillow-heif` is registered for HEIC/HEIF at import). Other Pillow plugins (TIFF, GIF, EPS, PDF, ...) are unreachable from an upload.
   - **Decompression-bomb guard:** the pixel count declared in the header is checked before any pixel data is decoded. Above `scan.max_image_pixels` in `ai_settings` (seeded from `SCAN_MAX_IMAGE_PIXELS`; default 40 MP, range 1–100 MP) → `413 image_too_large_pixels`. Pillow's own `DecompressionBombError` maps to the same response.
   - **Decode or reject:** the image is fully decoded; anything Pillow cannot decode (truncated, corrupt, wrong format behind valid magic bytes) → `415 image_undecodable`. There is no raw-bytes fallback: undecodable bytes never reach the provider.
   - EXIF orientation is applied, then a fresh image is built so all metadata (EXIF including GPS, XMP, IPTC, ICC profiles, comments) is dropped. Re-encoding also removes polyglot payloads (scripts in comment segments, archives after the end-of-image marker).
   - It runs in a worker thread, at most `SCAN_NORMALISE_CONCURRENCY` (default 1, range 1–8) at once per process, so a burst of large images cannot block the event loop or multiply peak memory.
   - The long edge is shrunk to the feature's `max_image_long_edge_px` (registry, 1568 px for `scan.standard` and `scan.enhanced`); smaller images are never upscaled. Shrinking happens before alpha is flattened onto white; every mode is converted to RGB, with 16-bit greyscale scaled to 8-bit rather than clipped.
   - Output is always JPEG, quality 85. Only these bytes are sent to the provider, which caps the input cost per image (about 3,000 tokens at 1568 px).
   - The log line carries numbers only (output width × height and byte counts), never the image, a filename or any metadata value.
7. **Result cache** (`app/ai/cache.py`, #534), before the quota reservations. The cache is per user: its key is the SHA-256 of the user id, the SHA-256 of the normalised bytes, the feature, the resolved model and a fingerprint of the request shape (provider, prompt as rendered, output schema, effort, token cap, adapter `PROMPT_VERSION`), all decided by the server; the lookup also filters on the user id. No request field, header or query names a key, a hash, a user, a model or a prompt version, and one user can never be served another user's entry for the same photo (`tests/security/test_result_cache_authority.py`). An enhanced hit is refused for a non-Premium user (`403 enhanced_requires_premium`) exactly like a miss. Hits are capped per user per day (`cache.max_hits_per_day`, default 3); past the cap a re-upload is a normal, counted scan. **What is stored about a photo:** only its SHA-256 (of the normalised JPEG) and the validated candidate list (title, author, confidence, ISBNs; at most `SCAN_MAX_BOOKS`, and only when at least one book was found), per user, for at most `cache.ttl_days` (30); never the image bytes and never the model's raw output. Rows go with the account (cascade) and expire after 30 days.

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

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
| CORS | Explicit method/header allowlist; wildcard origins rejected at config validation |
| Input validation | Pydantic on all endpoints; extension + MIME + size + magic bytes on uploads |
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
| JPEG magic + PHP body (polyglot) | Passes magic bytes; forwarded to OpenAI — bytes never executed server-side |
| `Content-Type: image/gif` with valid JPEG body | Passes MIME check, fails magic bytes → 415 |
| `Content-Type: application/octet-stream` | Fails MIME check immediately → 415 |
| Filename `../../../../etc/passwd.jpg` | Extension `"jpg"` extracted via `rsplit(".", 1)[-1]`; magic bytes validated |
| No extension: `filename: "file"` | `"." in filename` is False → 415 |
| File exactly 5 MB | Should pass |
| File 5 MB + 1 byte | 413 |

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

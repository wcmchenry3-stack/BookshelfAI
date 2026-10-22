# Features

What the app does from a user's perspective, with flow diagrams. For the REST surface backing each feature see [`api.md`](api.md); for the data model see [`data-model.md`](data-model.md).

## Screens

Navigation is file-based via `expo-router`. Two route groups:

### `(auth)` — unauthenticated stack
- **`login.tsx`** — Google Sign-In. The only screen reachable before auth. Tapping the Google button opens the system OAuth flow; on success the app exchanges the Google ID token for a Bookshelf access + refresh pair.

### `(tabs)` — authenticated tab bar
- **Scan** (`scan.tsx`) — camera viewfinder (native) or file picker (web). Primary path into the app.
- **My Books** (`my-books.tsx`) — the user's full library with a status filter (All / Wishlisted / Purchased / Reading / Read). Tapping a book opens its detail modal for notes, rating, status transitions, or delete.
- **Wishlist** (`wishlist.tsx`) — filtered view showing only `wishlisted` entries. One-tap "I bought this" transitions to `purchased`.
- **Settings** (`settings.tsx`) — account info, language picker, theme toggle, sign-out.

## Core flows

### Scan flow — camera → identification → wishlist

This is the primary user flow. A photo becomes an EnrichedBook candidate, gets confirmed, and lands in the wishlist.

```mermaid
sequenceDiagram
  participant U as User
  participant S as scan.tsx
  participant C as expo-camera / file input
  participant SJ as ScanJobContext
  participant FS as expo-file-system
  participant API as FastAPI /scan
  participant GPT as ChatGPT Vision
  participant GB as Google Books
  participant DB as PostgreSQL
  participant P as BookCandidatePicker

  U->>S: Tap capture
  S->>C: takePictureAsync() / File input
  C-->>S: photo URI
  S->>FS: Copy to scan-queue/ (native only)
  FS-->>S: persisted URI
  S->>SJ: startScan('image', uri)
  SJ->>SJ: Create ScanJob(id, status=pending)
  SJ->>API: POST /scan (multipart)
  API->>GPT: identify(base64 image)
  GPT-->>API: 3 candidates {title, author, confidence, isbn}
  API->>GB: enrich each (cover, work_id, description)
  GB-->>API: metadata
  API->>DB: dedup check vs user's UserBooks
  DB-->>API: already_in_library flags
  API-->>SJ: 3 EnrichedBook results
  SJ->>SJ: status=complete, show banner
  U->>P: Tap "View results"
  P->>U: Show 3 candidates with covers
  U->>P: Select one
  P->>API: POST /wishlist
  API->>DB: Create UserBook(status=wishlisted)
  DB-->>API: OK
  API-->>P: UserBook
  P-->>U: Confirmation + back to scan
```

**Key implementation notes:**
- Images are persisted to `scan-queue/` (expo-file-system) on native so the job survives app backgrounding. Web doesn't persist — blob URLs die on navigation.
- `ScanJobContext.startScan()` is wrapped in try/catch with Sentry capture; any sync throw (like a crypto/uuid failure) becomes a handled banner, not a crash.
- Scan job IDs are minted with `Crypto.randomUUID()` from `expo-crypto` (native-backed, Hermes-safe — see the uuid regression in `frontend/.eslintrc.js`).
- Text search (`/books/search`) uses the same `ScanJob` pipeline with `type='text'` — different input path, same state machine.

### Scan limits — what users see

Every user has a monthly and a daily scan allowance (standard and enhanced scans share it). The numbers come from `GET /scan/usage`; no copy hardcodes them. The month is the UTC calendar month and the reset date shown is the API's `period_end`, formatted for the user's locale.

- **Counter (scan screen):** "Scans used this month: X of Y" and "Scans left today: N" (the tighter of the daily and the monthly remainder). Refetched after every scan that reaches the server. Hidden while offline or if the request fails; it never shows an error.
- **Settings:** a "Scan usage" row with the same used / limit figure for the month. No plan label, renewal date or upgrade button.
- **Monthly limit (402 `scan_limit_reached`):** "You've used your N scans for this month. Your scans reset on <date>." No upgrade CTA.
- **Daily limit (429 `daily_scan_limit`):** "Daily limit reached. Scanning resets at midnight UTC (<local time> your time)."
- **Scan already running (429 `scan_in_progress`):** the app waits `Retry-After` (default 2 s, capped at 10 s) and retries silently, up to 3 times. If it is still busy: "Still processing. Please try again in a moment." (3 retries is about 6 s, long enough to ride out a normal overlap without leaving the user waiting on a stuck lease.)
- **Scanning off (503 `scan_disabled`, `enhanced_scan_disabled`, `scan_capacity`, `ai_unavailable`, `ai_budget_reached`, `scan_unavailable`):** "Scanning is paused. Please try again later."
- **Daily budget reached (503 `ai_budget_reached`, #531):** when the service's daily AI budget is used up, Free scans pause for the rest of the UTC day. Shown as "Scanning is paused" (same notice and queueing as above). Premium scans continue.
- 402 `no_enhanced_credits` is unchanged (enhanced re-scan falls back to the standard results).
- **Refused scans wait in the queue.** A scan refused for a monthly or daily limit, a paused service or "still processing" is not lost or marked failed: it goes back to `queued`, so it shows in the "Pending uploads" badge, survives an app restart and is sent by the next queue drain (reconnect or app launch). After the first refusal the drain sends no more photos, so a refusal costs at most one request and the remaining photos stay queued; queued text searches (which don't use the scan allowance) are still sent, and when only enhanced scans are switched off (`enhanced_scan_disabled`) standard photos are still sent.
- **Try again:** the notice has a "Try again" button next to "Dismiss" whenever scans are waiting. It clears the notice and sends all queued scans in order, stopping at the next refusal. It is not shown when a refused enhanced re-scan has put the earlier results back on screen (those results are restored, nothing is queued).
- **Send now:** the "Pending uploads: N" badge has a "Send now" button (online only) that does the same as "Try again", whether or not a notice is showing. Offline, "Try again" keeps the notice and shows the "requires an internet connection" message.
- **Automatic resume:** when a new scan succeeds, any scans that were refused earlier are sent right after (the service is evidently accepting scans again).
- **Queue full during a limit:** if the 50-photo queue is full while scanning is limited or paused, the capture-blocked message reads "You've reached your scan limit. Your saved photos will be sent when scanning is available again." instead of "Wait for pending scans to upload".
- Offline capture and the upload queue otherwise behave as before.

### Auth flow — Google SSO → rotating refresh tokens

```mermaid
sequenceDiagram
  participant U as User
  participant L as login.tsx
  participant G as Google OAuth
  participant API as FastAPI /auth
  participant SS as SecureStore
  participant DB as refresh_tokens

  U->>L: Tap "Sign in with Google"
  L->>G: Open OAuth flow (platform-native)
  U->>G: Approve
  G-->>L: Google ID token (JWT)
  L->>API: POST /auth/google {id_token}
  API->>G: Verify token (JWKS, check aud ∈ configured client IDs)
  G-->>API: Valid
  API->>DB: Create/update User; insert RefreshToken(jti)
  API-->>L: {access_token, refresh_token}
  L->>SS: Store both (rule #6: SecureStore only, never AsyncStorage)
  L->>U: Navigate to (tabs)

  Note over U,API: ... 24 hours pass, access token expires ...

  U->>API: Any authenticated request
  API-->>U: 401 Unauthorized
  U->>API: POST /auth/refresh {refresh_token}
  API->>DB: Check jti exists + not revoked
  DB-->>API: Valid
  API->>DB: Mark old jti revoked; insert new jti
  API-->>U: {access_token, refresh_token} (new pair)
  U->>SS: Update stored tokens
```

**Key properties:**
- `verify_google_id_token()` accepts tokens whose `aud` is any of the three configured client IDs (web, iOS, Android) — one API serves all platforms.
- Refresh tokens are **single-use**: the old `jti` is revoked the instant a refresh happens. A stolen refresh token is invalidated by the legitimate client's next refresh.
- `ALLOWED_EMAILS` lockdown rejects any authenticated email not in the allowlist (single-user app by default).
- Token storage: `expo-secure-store` only, never `AsyncStorage` (rule #6).

## Reading-state transitions

A `UserBook` moves through four states. Transitions auto-timestamp the corresponding column (`wishlisted_at`, `purchased_at`, `started_at`, `finished_at`).

```
   wishlisted ─▶ purchased ─▶ reading ─▶ read
       │             │           │
       │             │           └─▶ reading (re-read, updates started_at)
       │             │
       │             └─▶ (no step back to wishlisted)
       │
       └─▶ dismiss (delete UserBook entirely)
```

Non-canonical transitions (e.g. skipping states, re-reads) are allowed — the frontend UI exposes the common paths, but the API accepts any state value on `PATCH /user-books/{id}`.

## Offline resilience

Scan jobs persist locally so the user can keep scanning even without connectivity. `ScanJobContext` is the single source of truth.

**Persistence:**
- Every change to the jobs list is saved to `expo-file-system` via `saveJobs()` (`lib/scanJobStorage.ts`).
- On app launch, `loadJobs()` restores the queue. Any job stuck in `searching` status (because the app was killed mid-scan) is reset to `pending`.

**Queue draining:**
- `NetInfo.addEventListener` watches for connectivity changes.
- When the app comes online, `drainQueue()` picks up any `queued` jobs and retries them, with a `QUEUE_DRAIN_DELAY` (2s) between each to avoid thundering the backend.
- `MAX_RETRIES` (3) caps how many times a single job auto-retries before it's parked in `queued` status for manual retry.

**Job state machine:**
```
pending ─▶ searching ─▶ complete
             │             │
             │             └─▶ (user confirms) ─▶ deleted
             │
             ├─▶ queued (refused: limit / paused / busy; next drain or "Try again")
             └─▶ failed ─▶ queued (manual)
                    │
                    └─▶ (user taps retry, if retries < 3) ─▶ pending
```

## i18n

**11 active locales** + 2 reserved for future RTL work.

| Code | Language | Status |
|---|---|---|
| `en` | English | Bundled statically (always in initial bundle) |
| `fr-CA` | French (Canadian) | Lazy |
| `es` | Spanish | Lazy |
| `hi` | Hindi | Lazy |
| `zh` | Chinese (Simplified) | Lazy |
| `ja` | Japanese | Lazy |
| `ko` | Korean | Lazy |
| `pt` | Portuguese | Lazy |
| `de` | German | Lazy |
| `nl` | Dutch | Lazy |
| `ru` | Russian | Lazy |
| `ar` | Arabic | Translations present, **disabled until RTL layout mirroring lands** |
| `he` | Hebrew | Translations present, **disabled until RTL layout mirroring lands** |

**Namespaces** (8): `common`, `auth`, `tabs`, `my-books`, `scan`, `settings`, `wishlist`, `components`. Each screen/context pulls only what it needs.

**Loading strategy:**
- `en` is a static import — always in the initial bundle, no flash-of-keys on first render, tests work without async setup.
- All other locales are lazy via `i18next-resources-to-backend` with static `import()` paths (Metro requires them static; see the explicit `importMap` in `src/i18n/i18n.ts`).
- Only the active locale's 8 namespaces are fetched at runtime — 80 of 88 JSON files stay deferred.

**Adding a string** (rule #9: every user-facing string must be translated):
1. Add the key to `src/i18n/locales/en/<namespace>.json`
2. Add the same key to the `_meta/<namespace>.meta.json` file with description + context
3. Translate into all 10 active non-en locales (or mark for translation and ship English fallback)
4. Use in component: `const { t } = useTranslation('<namespace>'); t('key')`

## Rules referenced

- Rule #5: No hardcoded colours — use `useTheme()` (settings screen has theme toggle)
- Rule #6: `SecureStore` only for tokens — never `AsyncStorage` (auth flow)
- Rule #9: i18n required on every user-facing string

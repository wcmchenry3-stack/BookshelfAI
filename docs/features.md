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
  API->>GB: enrich each (title, author, cover, work_id, description)
  GB-->>API: metadata (the provider's title/author replace the AI's reading)
  API->>DB: dedup check vs user's UserBooks
  DB-->>API: already_in_library flags
  API-->>SJ: 3 EnrichedBook results (each with catalog_token, #602)
  SJ->>SJ: status=complete, show banner
  U->>P: Tap "View results"
  P->>U: Show 3 candidates with covers
  U->>P: Select one
  P->>API: POST /wishlist (the book as received, catalog_token included)
  API->>DB: Verify token; create missing Book/Edition only if valid; create UserBook(status=wishlisted)
  DB-->>API: OK
  API-->>P: UserBook
  P-->>U: Confirmation + back to scan
```

**Key implementation notes:**
- Images are persisted to `scan-queue/` (expo-file-system) on native so the job survives app backgrounding. Web doesn't persist — blob URLs die on navigation.
- `ScanJobContext.startScan()` is wrapped in try/catch with Sentry capture; any sync throw (like a crypto/uuid failure) becomes a handled banner, not a crash.
- Scan job IDs are minted with `Crypto.randomUUID()` from `expo-crypto` (native-backed, Hermes-safe — see the uuid regression in `frontend/.eslintrc.js`).
- **The title shown is the catalog's (#602).** The AI's reading of the photo is only used to look the book up. When Open Library or Google Books finds it, the result shows (and the wishlist stores) their title and author, so a mis-read or doctored cover cannot rename a book for everyone. When neither finds it, the result shows what the AI read, but it cannot be added to the wishlist (the app shows its "couldn't save" message). Results saved for a re-scan before this change are looked up again on the next re-scan (still free, no AI call).
- Text search (`/books/search`) uses the same `ScanJob` pipeline with `type='text'` — different input path, same state machine.

### Scan limits — what users see

Every user has two daily allowances (#577). The numbers come from `GET /scan/usage`; no copy hardcodes them. The day is the UTC day.
- **Scans** (the found counter): scans where the AI found books, plus anything below that used a scan. Standard and enhanced scans share it. Free 5 a day, Premium 5 (operator settings `quota.<tier>.found_daily`).
- **Unrecognised photos** (the no-match counter): photos the AI read properly but found no book in. While this allowance lasts, such a photo does not use a scan, so an unlucky photo doesn't cost one. Free 5 a day, Premium 5 (`quota.<tier>.no_match_daily`). Once it is used up, a photo with no book found uses a scan.
- A scan is refused only when the scans allowance is used up, even if unrecognised photos are left (the next photo might find books). Only real AI calls count: a re-uploaded photo answered from its saved record, an AI reply that could not be read, and an AI outage never use either allowance. Enhanced scans that find nothing still use a scan (the enhanced rule below).

**There is no monthly limit (#582, owner decision).** No scan is ever refused for a monthly reason, and the app shows no monthly counter, no monthly-limit message and no "resets on" date. Daily limits keep cost bounded: each user's daily limit, the service-wide daily cap, the daily spending cut-off ("Scanning is paused" below) and the monthly spending limit set in the AI provider's console. Monthly spend and usage are still tracked for reporting, not as a limit.

- **Counter (scan screen):** "Scans left today: N" (the scans allowance left) and, under it, "Unrecognised photos: N left" (the no-match allowance left; hidden when an older server does not send it, #577). Refetched after every scan that reaches the server. Hidden while offline or if the request fails; it never shows an error.
- **No book found (#577):** the "No books found" banner says "No books found. This photo didn't use a scan." when the photo went to the unrecognised-photo allowance (`no_match: "free"`), or "No books found. This used a scan (unrecognised-photo allowance used up today)." when that allowance was already used up (`no_match: "counted"`). Without the field (an older server, a repeat, an enhanced scan) it is the plain "No books found".
- **Repeat photos (#588):** when the server says the photo was scanned before (`repeat: true`, #585), the app explains it instead of showing a normal scan, using `in_library` on each book. Nothing is charged and the usage counters are not refetched. No AI or Google Books call is made.
  - Every book already in the library: "You already have this book." / "You already have these books." The photo is discarded and no review sheet opens.
  - Some books not in the library (rejected, deleted, or a save that failed): "You scanned this before and it's not in your library. Re-add it?" with a **Re-add** button. It opens the usual review sheet on the saved result, which preselects only the books not in the library; confirming sends `POST /wishlist` only, never another `/scan`.
  - A photo where no book was found before: "We couldn't find a book in this photo before. Try a clearer photo or a different angle." The Premium enhanced-scan offer still shows when it normally would.
  - An enhanced re-scan of a photo already enhanced is a repeat too and gets the same messages; an empty one keeps the enhanced "no books found" message. Text searches are never repeats.
- **Settings:** a "Scan usage" row with the same two figures. No plan label, renewal date or upgrade button.
- **An older server's monthly refusal (402 `scan_limit_reached` / `enhanced_limit_reached`):** shown as the generic "Scan failed" banner (retry or save for later), or for an enhanced re-scan "Enhanced scan failed. Showing your earlier results."; never a monthly message.
- **Daily limit (429 `daily_scan_limit`):** "Daily limit reached. Scanning resets at midnight UTC (<local time> your time)."
- **Daily request limit (429 `scan_requests_daily_limit`, #586):** each user can send at most 100 scans a day (`scan.requests_per_user_daily`, changeable without a release), counting every photo sent while scanning is switched on, including repeats of a photo already scanned (which are otherwise free) and photos that were refused. It stops anyone hammering the server with repeat photos; normal use stays far below it. The app shows "You've reached today's scan limit. Try again after <local time>." (the limit resets at midnight UTC), keeps the photo in the queue like the daily limit, and sends it on "Try again" or the next drain.
- **Scan already running (429 `scan_in_progress`):** the app waits `Retry-After` (default 2 s, capped at 10 s) and retries silently, up to 3 times. If it is still busy: "Still processing. Please try again in a moment." (3 retries is about 6 s, long enough to ride out a normal overlap without leaving the user waiting on a stuck lease.)
- **Scanning off (503 `scan_disabled`, `enhanced_scan_disabled`, `scan_capacity`, `ai_unavailable`, `ai_budget_reached`):** "Scanning is paused. Please try again later."
- **Scan queue screen (#605, `frontend/app/scan-queue.tsx`, route `/scan-queue`):** a screen outside the tabs, opened from the "View queue" button on the scan screen's pending-uploads badge (also offline) and from the "View queue" action on the final-failure banner (which keeps its "Retry" action). **Waiting** lists every queued, pending or sending job with its photo thumbnail and state: "Waiting for connection", "Next try at <local time>" (from `nextAttemptAt`) or "Sending…". **Failed** lists failed jobs with a thumbnail, when they failed, and **Retry** (`restartScanSchedule`: the photo goes back to attempt 1 with a fresh schedule and is sent now, still subject to every server limit) and **Delete** (asks "Delete this scan? The photo will be removed." first; on confirm `dismissJob` removes the job and its image file). Retry follows the one-photo-at-a-time rule (#604): if another photo is being sent or waiting on its schedule, the retried photo is queued and sent in turn (the banner's Retry too). Each button's screen-reader label includes when the scan failed. "Next try at" refreshes every 30 s while the screen is open. The scan screen shows a "View queue (N failed)" link when only failed scans exist. Text searches are listed too, with their query in place of a thumbnail. Each section has an empty state. Failed scans are kept 7 days (`FAILED_SCAN_RETENTION_MS` in `frontend/lib/scanJob.ts`) and are then removed with their image, on app launch and whenever the screen opens; the time counts from `failedAt` (set when a job becomes failed), or from `createdAt` for jobs saved without it (older saved jobs).
- **AI unavailable: automatic retries (503 `scan_unavailable`, #604):** when the AI provider is out, timed out, overloaded or sent a malformed reply (#572), the server has already retried once and refunded the scan. The app then retries the photo by itself on a fixed schedule, counted per photo: attempt 2 after 30 s, attempt 3 after 10 min, attempt 4 after 1 h, attempt 5 after 4 h (`SCAN_RETRY_DELAYS_MS` in `frontend/lib/scanErrors.ts`). Each request sends a form field `attempt` (1–5). While the first retry is pending the scan screen shows the "Scanning is paused" notice (without "Try again"); later retries show nothing. The photo stays in the queue with its attempt count and next try time saved on the job (`attempts`, `nextAttemptAt`, for the queue screen in #605), so the schedule survives a restart; while the app is open a timer sends it when due, and a step that falls due while the app is closed is sent on the next launch. While a photo waits for its next attempt, no other queued photo is sent (one at a time); queued text searches still go, and a new capture is still sent straight away (a capture is a user action; if it also gets `scan_unavailable` it starts its own schedule). Any successful scan (a new capture or a queued photo) means the service is back: every photo waiting on the schedule is sent right away, keeping its attempt count. No wait is ever longer than 4 h, even if the saved next try time is further off (the clock moved back). Offline, the schedule pauses: nothing is sent and no attempt is used, and on reconnect (or a check every minute) a photo whose wait has passed is sent. A request with no response is not a failed attempt either, but the photo waits 30 s before it is sent again. Only a known-offline connection counts as offline; an unknown connection state is treated as online. If attempt 5 also fails, the photo is **failed**, one banner says "This scan failed. Tap Retry to try again." with a "Retry" button that sends it again with the schedule restarted at attempt 1, one Sentry message (fingerprint `scan-final-failure`, tags for the attempt count and last error code, no photo or user content) is sent, and the rest of the queue is sent. The failed photo stays on the device, and the banner also has a "View queue" button that opens the scan queue screen (below). Nothing else is retried: no books found, refusals, any 4xx, `ai_budget_reached` / paused and the kill switches keep their own handling. An enhanced re-scan is never retried either: as before, it gets the paused notice and its standard books come back, or, with none, it waits in the queue like a paused scan (no final-failure banner or Sentry message).
- **Daily budget reached (503 `ai_budget_reached`, #531):** when the service's daily AI budget is used up, Free scans pause for the rest of the UTC day. Shown as "Scanning is paused" (same notice and queueing as above). Premium scans continue.
- **Enhanced scans (#533)** are a Premium feature. They replace the old lifetime "enhanced credits":
  - The "Try enhanced scan" offer (after a photo finds no books, and in the results sheet) appears only when the server reports enhanced scans left (`enhanced_scans_remaining` on the scan response: since #582 the Premium daily allowance left) and that number is above 0. Free users (`null`) never see it.
  - There is no enhanced monthly allowance (#582). An enhanced scan uses one of the shared daily scans, like any scan, whether or not the AI finds books; the daily allowance is what limits enhanced scans until #583 adds a separate daily limit for the stronger model.
  - An enhanced scan where the AI finds no books is recorded; after a few of them in a short time (`scan.enhanced.zero_result_threshold`, default 3, within `scan.enhanced.zero_result_window_hours`, default 24) a warning is raised for the operator and, if the operator chose `throttle`, enhanced scans are refused until the window clears. This stops repeated empty enhanced scans from running the stronger model for nothing.
  - What the app shows when an enhanced scan is refused (no notice card; a banner, and the standard scan's books come back if there were any): not Premium (403 `enhanced_requires_premium`): "Enhanced scans are part of Premium."; throttled (429 `enhanced_throttled`): "Several recent enhanced scans found no books. Try again after <local time from Retry-After>." The offer is then hidden until the server reports enhanced scans left again. These refusals stop only enhanced photos in a queue drain; standard photos are still sent.
- **Re-scanning the same photo is free (#585).** Every photo is fingerprinted, and for each user the service keeps a scan record for 30 days (`scan_records.retention_days`): the AI's answer, the book details from Google Books / Open Library, and which of those steps finished. Re-uploading the exact same photo returns the saved result with no AI call, no Google Books call and no scan used (the daily counter does not move), and the response says it is a repeat (`repeat: true`). There is no daily cap on repeats. If the AI answered last time but the book lookup failed, only the lookup is redone, still free. Each book also says whether it is already in the user's library (`in_library`), checked again on every scan. "The same photo" means the same file after the server's normalisation; a different crop or a new shot of the same shelf is a new photo. A photo where no books were found is saved too (#572), so re-uploading it returns the same "no books" answer for free. A reply from the AI that could not be read (missing, garbled, or cut short before a single complete book) is never "no books found": the user gets a "try again" error, no scan is used, and nothing is saved, so the next upload is a normal scan (#572). A reply cut short after some complete books returns those books and uses a scan, as before, but is not saved. A readable reply in which some books have a blank title or author is still an answer: those books are skipped (Claude) or looked up by title alone (OpenAI), as before. An enhanced scan of a photo first scanned as standard runs the stronger model (and its answer replaces the saved one); enhanced scans stay Premium-only. Records are per user: one user's results are never shown to another. The app copy for repeats is #588.
- **Refused scans wait in the queue.** A scan refused for the daily limit, a paused service or "still processing" is not lost or marked failed: it goes back to `queued`, so it shows in the "Pending uploads" badge, survives an app restart and is sent by the next queue drain (reconnect or app launch). After the first refusal the drain sends no more photos, so a refusal costs at most one request and the remaining photos stay queued; queued text searches (which don't use the scan allowance) are still sent, and when only enhanced scans are switched off or refused (`enhanced_scan_disabled`, or the enhanced refusals above) standard photos are still sent.
- **Try again:** the notice has a "Try again" button next to "Dismiss" whenever scans are waiting (but not while a photo waits on the retry schedule above, when it would send nothing). It clears the notice and sends all queued scans in order, stopping at the next refusal. It is not shown when a refused enhanced re-scan has put the earlier results back on screen (those results are restored, nothing is queued).
- **Send now:** the "Pending uploads: N" badge has a "Send now" button (online only) that does the same as "Try again", whether or not a notice is showing. Offline, "Try again" keeps the notice and shows the "requires an internet connection" message.
- **Automatic resume:** when a new scan succeeds, any scans that were refused earlier are sent right after (the service is evidently accepting scans again).
- **Queue full during a limit:** if the 50-photo queue is full while scanning is limited or paused (or a photo waits on the retry schedule, #604), the capture-blocked message reads "You've reached your scan limit. Your saved photos will be sent when scanning is available again." instead of "Wait for pending scans to upload".
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
- On app launch, `loadJobs()` restores the queue. Any job stuck in `searching` status (because the app was killed mid-scan) is reset to `pending`. A job on the retry schedule (#604) keeps its `attempts` and `nextAttemptAt`.

**Queue draining:**
- `NetInfo.addEventListener` watches for connectivity changes.
- When the app comes online, `drainQueue()` picks up any `queued` jobs and retries them, with a `QUEUE_DRAIN_DELAY` (2s) between each to avoid thundering the backend.
- `MAX_RETRIES` (3) caps how many times a single job auto-retries before it's parked in `queued` status for manual retry.
- Retry schedule (#604): a photo that got 503 `scan_unavailable` waits in `queued` until its `nextAttemptAt`. The drain sends due photos first and sends no other photo while one is still waiting; a timer drains when the earliest one is due (cleared on unmount, never sending a job that is in flight, never waiting longer than 4 h, and checking again every minute while offline). A successful scan makes every waiting photo due now. After a final failure the rest of the queue is sent. After attempt 5 the photo is `failed`.

**Job state machine:**
```
pending ─▶ searching ─▶ complete
             │             │
             │             └─▶ (user confirms) ─▶ deleted
             │
             ├─▶ queued (refused: limit / paused / busy; next drain or "Try again")
             ├─▶ queued + nextAttemptAt (503 scan_unavailable, attempts 1-4; sent when due)
             │      └─▶ failed (attempt 5 failed; banner "Retry" ─▶ pending, attempt 1)
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

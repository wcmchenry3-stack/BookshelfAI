# Features

What the app does from a user's perspective, with flow diagrams. For the REST surface backing each feature see [`api.md`](api.md); for the data model see [`data-model.md`](data-model.md).

## Screens

Navigation is file-based via `expo-router`. Two route groups:

### `(auth)` — unauthenticated stack

- **`login.tsx`** — Google Sign-In. The only screen reachable before auth. Tapping the Google button opens the system OAuth flow; on success the app exchanges the Google ID token for a Bookshelf access + refresh pair. If the OAuth prompt fails to start (network error, blocked popup) or Google returns an error, an inline localized error with a "Try again" button is shown instead of an unhandled promise rejection.

### `(tabs)` — authenticated tab bar

- **Scan** (`scan.tsx`) — camera viewfinder (native) or file picker (web). Primary path into the app.
- **My Books** (`my-books.tsx`) — the user's full library (#930). A serif **My Books** title with a "10 books · 2 reading now" subtitle (the reading part is left out at 0), a **Title or author** search field (case-insensitive, with a clear button) and filter chips with counts (All / Wishlist / Purchased / Reading / Read; the selected chip scrolls into view). With the All chip and no search, a **Reading now** shelf of covers (title and author under each, tap to open the detail modal) sits above an **All books** list that shows its current sort; the **Sort** button opens a "Sort by" sheet (Recently added, Title A–Z, Author A–Z; titles are not re-ordered around leading articles) and the choice is remembered on the device (#933). A **Show as grid** button in the header switches the list to a grid of covers (3 across on a phone, more on a wider window; title and status under each; no shelf and no ⋯ menu, tap a cover for the detail modal) and **Show as list** switches back; the view is remembered on the device too (#934). The app makes one unfiltered `GET /user-books` per focus (also on a library change and on pull to refresh); the counts, chip filter and search are all worked out on the device, so choosing a chip makes no request. Empty states: an empty library ("Scan books" goes to Scan), a chip with no books ("Show all books"), a search with no match ("Clear search"), and a failed first load ("Try again"; it is never shown as an empty library). Tapping a book opens its detail modal for notes, rating, status transitions, or delete. Since #683 a delete moves the entry to the server's trash (restorable for 30 days; deleting a book again replaces older trashed copies that hold no data of yours, such as notes, a rating, an edition, overrides or a status beyond wishlisted, while copies with your data stay restorable for 30 days; newest 1,000 kept); the app's Trash screen (below) lists and restores them. The app says so after a delete: a banner reads "Moved to Trash. You can restore it within 30 days." with a **View trash** action (My Books and Wishlist). **Changed on another device (#685):** a status change or delete sends the version of the entry the list showed (`If-Match`); when the book was changed on another device since (`412 version_mismatch`), nothing is changed, the banner reads "This book was changed on another device. Showing the latest version.", the detail sheet closes, My Books and Wishlist reload, and the change is not retried (the user can redo it on the fresh copy). It is not reported to Sentry.
- **Wishlist** (`wishlist.tsx`) — filtered view showing only `wishlisted` entries (#931). A serif "Wishlist" header with a count ("3 books you want to buy"), a search field that matches title and author (ignoring case and accents; a search with no match keeps the header and field and offers **Clear search**), and the offline notice above the list. One-tap "Bought" transitions to `purchased`. An empty wishlist shows an illustration, "Nothing on your wishlist yet", **Scan books** (opens Scan) and **Search by title** (opens Scan already in text search, through the `?mode=text` route param, which Scan clears after use).
- **Trash (#683, `frontend/app/trash.tsx`, route `/trash`):** a screen outside the tabs, opened from the **Trash** row in Settings and from the **View trash** action on the banner shown after a delete. Lists the entries in the trash (`GET /user-books/trash`, newest deletion first) as library rows (#932) with cover, title, author, an Unverified badge where it applies and "Deleted for good in N days" (or "today"), counted in local calendar days from `purge_after` and shown in the danger colour at 3 days or fewer. A compact top row has **Back** and the "Trash" title; an info card says removed books stay 30 days, then are deleted for good (the backend still replaces older copies without notes or rating and keeps only the newest 1,000). **Restore** (`POST /user-books/{id}/restore`) removes the row and shows a "Restored <title> to your library." banner; A successful restore also fires the "library changed" signal (`frontend/lib/libraryChanged.ts`), so My Books and Wishlist reload even if the restore finished after they regained focus (they also reload on focus). A refresh that was in flight during a restore never brings the restored row back. Errors show as error banners (they work on web, where `Alert` does not): `409 restore_conflict` "This book is already in your library."; `409 unverified_limit_reached` shows the same limit message as the add-it-yourself form; `404` reloads the list ("This book is no longer in the trash."); anything else "Couldn't restore this book. Try again." (a `403 forbidden` shows the service-unavailable message). Restore is disabled offline. A failed refresh while rows are shown keeps them and shows "Couldn't refresh the trash." Has loading, error (with **Try again**), empty ("Trash is empty" plus the 30-day rule) and pull-to-refresh states. Restore is a small tonal chip on each row. The strings live in the `my-books` namespace (`trash.*`, `movedToTrash`, `viewTrash`) and `settings` (`trash`, `trashA11y`, `trashHint`).
- **Settings** (`settings.tsx`) — account info, language picker, theme toggle, scan usage,
  sign-out (confirm prompt; best-effort `POST /auth/logout` with the refresh token, then local
  sign-out and redirect to login). Server-side revocation is best-effort: if it times out (5 s)
  or the device is offline, the refresh token stays valid server-side until it expires
  (7 days). "Sign out of all devices" is the remedy and does not exist yet. A **Send feedback** row opens the feedback sheet (see Feedback below).

## Core flows

### Feedback

The Settings **Send feedback** row opens the bottom sheet (the header "?" button was removed in #936). The user picks Bug or Feature request and writes a description (max 2000 characters). Submit sends via Sentry User Feedback (`source: in_app_feedback`, tag `feedback.type`); session logs are attached for bugs only (lines cut at 1,000 characters, at most 100 KB, with Bearer/Authorization values, JWTs, sensitive URL params and email addresses redacted). Submissions are limited to 5 per 10 minutes per install. On success the sheet closes and a 4 s "Thanks for your feedback!" banner shows. Strings live in the `feedback` namespace.

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
  participant AI as Claude vision
  participant GB as Google Books
  participant DB as PostgreSQL
  participant P as BookCandidatePicker

  U->>S: Tap capture
  S->>S: AI consent (#1023): ask with the prompt unless it stands; Not now stops here
  S->>C: takePictureAsync() / File input
  C-->>S: photo URI
  S->>FS: Shrink (1568 px, JPEG 0.8), copy to scan-queue/ (native only)
  FS-->>S: persisted URI
  S->>SJ: startScan('image', uri)
  SJ->>SJ: Create ScanJob(id, status=pending)
  SJ->>SJ: upload gate: consent must stand (else the photo waits, queued)
  SJ->>SJ: getToken(): fresh Turnstile token (#740; none if unset/timeout)
  SJ->>SJ: AI consent re-check, then device attestation (#870; native release builds, 5 s max, none on failure)
  SJ->>API: POST /scan (multipart, cf-turnstile-response, attestation-* fields)
  API->>AI: identify(base64 image)
  AI-->>API: 3 candidates {title, author, confidence, isbn}
  API->>GB: enrich each (title, author, cover, work_id, description)
  GB-->>API: metadata (the provider's title/author replace the AI's reading)
  API->>DB: library check vs user's UserBooks (by edition, #587)
  DB-->>API: ownership per book
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

- The scan queue (jobs, results and photos) is cleared on sign-out, so another account never sees or uploads it; in-flight scans, searches and wishlist adds are aborted and their results dropped (#723; remaining bounding work in #669).
- Images are shrunk at capture (1,568 px long edge, JPEG 0.8, #668) and persisted to `scan-queue/` (expo-file-system) on native so the job survives app backgrounding. Web doesn't persist — blob URLs die on navigation.
- `ScanJobContext.startScan()` is wrapped in try/catch with Sentry capture; any sync throw (like a crypto/uuid failure) becomes a handled banner, not a crash.
- Scan job IDs are minted with `Crypto.randomUUID()` from `expo-crypto` (native-backed, Hermes-safe — see the uuid regression in `frontend/.eslintrc.js`).
- **The title shown is the catalog's (#602).** The AI's reading of the photo is only used to look the book up. When Open Library or Google Books finds it, the result shows (and the wishlist stores) their title and author, so a mis-read or doctored cover cannot rename a book for everyone. When neither finds it, the result shows what the AI read and cannot be added to the shared catalog; the user can add it themselves as an unverified entry instead (#616, below). Results saved for a re-scan before this change are looked up again on the next re-scan (still free, no AI call).
- Text search (`/books/search`) uses the same `ScanJob` pipeline with `type='text'` — different input path, same state machine.
- **Books you already have (#587).** Every scan and search result says whether the book is already in your library, by edition (`ownership`; the edition compared is the one the app would add). The results sheet shows "You already have this book" (the same edition, or when there is no ISBN to compare) or "You already have this book in another edition". Those books can't be ticked, preselected or picked by "Select all", since a library holds one entry per title; there is no "Add anyway" (#610). If one is sent anyway (an older saved result, or the library changed in between), `POST /wishlist` answers `409` and the app counts it as already in the library, not a failure: one book shows `"Dune" is already in your library.`, several `2 books are already in your library.`, and a mix `Added 2. 1 already in your library.`; the review closes and nothing is retried or reported to Sentry. Results saved before the server sent `ownership` fall back to the work-level "Already owned" badge.
- **Books the catalog can't take (#612).** `POST /wishlist` answers `422 book_not_in_catalog` when a book has no Open Library / Google Books match (the server won't create shared catalog rows for it), and `422 catalog_token_invalid` when a scan or search result is stale or expired. The app shows the specific reason instead of the generic failure: `We couldn't find this book in our catalog. Search by title, or add it yourself.` or `This result is out of date. Scan or search for the book again.` (plural forms for several books). These books are counted as not saved in a mixed batch (for example `Added 1, 2 not saved. 1 not in our catalog: try searching by title. Tap Add to retry the others.`; the "Added" part is left out when nothing was added, and each reason carries its count). These banners show the whole text and stay longer than the usual 5 s, are never retried (only other failures stay in the review for Add) and are not reported to Sentry. Whenever a banner includes books not in the catalog it carries an **Add it yourself** action (#616, below).
- **Banner persistence (#909, `components/InAppBanner.tsx`).** Every banner clears when the route changes. An `error` banner with an action stays until acted on or dismissed. On iOS and Android any banner with an action also stays while a screen reader is on (web never holds). Otherwise a banner with an action times out after its `duration` or 10 s, and one without after its `duration` or 5 s. It floats just below the safe-area inset (inset + 8) on every screen.
- **Add it yourself — unverified entries (#616).** A book neither Open Library nor Google Books finds can be saved as the user's own **unverified** entry. Two ways in: the **Add it yourself** action on the `book_not_in_catalog` banner (one form per refused book, in turn), and an **Add it yourself** button on a result in the review sheet that no catalog matched ("Not in our catalog"). The sheet closes without discarding the scan, and the form opens once it has gone (`hooks/useModalHandoff.ts`: on iOS after the sheet's `onDismiss`, so two sheets are never presented at once). When the form closes the review reopens on the scan's remaining books: a saved book leaves the scan, a cancelled one stays, and a scan whose last book was saved is discarded. When an add from the sheet has mixed results, books the catalog refused stay in the open sheet with their button. The form (`components/AddUnverifiedBookSheet.tsx`, mounted globally in `app/_layout.tsx` from `ScanJobContext.unverifiedDrafts`) is pre-filled with the title, author and ISBN the scan read (`lib/unverified.ts` `draftFromBook`); the user corrects them and taps Save, which posts `POST /wishlist/unverified`. A blank title or author is caught before sending; the server's `409 unverified_duplicate` ("You've already added this book yourself."), `409 unverified_limit_reached` (the cap, default 200), an invalid ISBN and other failures show in the form (`unverifiedFailure`). On success the banner says `Added <title> to your wishlist as unverified.` The entry is visible only to the user and shows an **Unverified** badge (`components/UnverifiedBadge.tsx`) in My Books (list and detail) and the wishlist. Linking an unverified entry to a catalog book found later is out of scope.

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
- **Photo too large (413, #668):** scan photos are shrunk at capture to a 1,568 px long edge at JPEG quality 0.8 (never upscaled, upright, no EXIF; `frontend/lib/scanPhoto.ts`), about 300 to 600 KB each instead of 2.5 to 10 MB. The web picker shrinks the picked photo the same way (canvas, via `expo-image-manipulator`); if the browser cannot, the original is sent. If the server still refuses a photo with any 413 (`image_too_large_pixels`, "Image must be 5MB or smaller", "Request body too large"), the photo is failed for good: it is not retried and counts nothing. Banner: "This photo is too large to scan. Take a new photo and try again." (no Retry action); on the queue screen the failed row says "Too large to scan. Delete it and take a new photo." with Delete only. A too-large enhanced re-scan keeps the standard scan's books.
- **Scanning off (503 `scan_disabled`, `enhanced_scan_disabled`, `scan_capacity`, `ai_unavailable`, `ai_budget_reached`):** "Scanning is paused. Please try again later."
- **Bot check: Turnstile (#740):** every `POST /scan` request (first try, silent `scan_in_progress` retry, queued photo, scheduled retry) carries a fresh Cloudflare Turnstile token as `cf-turnstile-response`, from an invisible widget (a hidden WebView on iOS/Android, Cloudflare's script on web; no UI, no new copy). The wait for a token is at most 10 s; without a site key (`EXPO_PUBLIC_TURNSTILE_SITE_KEY`), on a timeout or on a widget error the request is sent without one, which works while the server has `TURNSTILE_REQUIRED=false`. When the server refuses the token (`400 "Missing Turnstile token"`, `403 "Turnstile verification failed"`), the photo is retried on the same schedule and caps as `scan_unavailable` above (same notice, same final-failure banner), each attempt with a new token; an enhanced re-scan takes the paused path instead, as for `scan_unavailable`. A build without a site key cannot send a token, so for it a `400 "Missing Turnstile token"` is the ordinary "Scan failed" banner at once (Retry now / Save for later), not the schedule. See [`security.md` → Turnstile on /scan](security.md#turnstile-on-scan).
- **Device attestation (#870):** iOS and Android release builds also attest each `POST /scan` (first try and every retry, each with a fresh server nonce): App Attest on iOS (a key is registered once with `POST /attestation/register` and kept in `SecureStore`; each scan sends an assertion), Play Integrity on Android (a token per scan; needs `EXPO_PUBLIC_PLAY_INTEGRITY_CLOUD_PROJECT_NUMBER`). It runs after the AI consent check, takes at most 5 s, and is invisible: no UI, no copy. If it fails or is unavailable (simulator, dev build, web, unsupported device, timeout, server not set up) the scan is sent without it and the Turnstile check above applies; a `403 "Attestation verification failed"` re-sends the scan once without attestation (and, on iOS, forgets the key so the next scan registers again). See [`security.md` → Device Attestation on /scan](security.md#device-attestation-on-scan).

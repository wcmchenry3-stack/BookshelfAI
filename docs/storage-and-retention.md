# Storage and retention

The one reference for how much the app keeps, where, and for how long: on the phone (queued and failed photos, job metadata, logs) and in the database (every table, its purge, expected growth). Written for #576. It is an investigation: the numbers marked *estimate* come from reading the code and schemas, not from measuring a device or the production database. Follow-up work is listed under [Gaps and follow-ups](#gaps-and-follow-ups); none of it is built yet.

Related: [`scan-limits.md`](scan-limits.md) (every scan limit), [`data-model.md`](data-model.md) (schemas and per-table volume notes), [`backups.md`](backups.md), [`security.md`](security.md#logging).

## Summary: the standard

- **Every table that grows with usage has a retention rule and a purge** run by the daily cleanup task (`backend/app/services/token_cleanup.py:46-115`), in bounded batches (5,000 rows, at most 20 batches per table per run). A new usage-driven table adds its purge there and a row in the [retention table](#table-by-table-retention) in the same PR; `backend/tests/unit/test_storage_retention_doc.py` fails if a model table is missing from this page.
- **Only three kinds of table may grow without a purge**: user-owned library data (`users`, `user_books`, `user_custom_books`, each capped or tied to a person; their live rows are kept, and rows in the trash are purged 30 days after deletion, #683), the shared catalog (`books`, `editions`, written only from signed provider data) and configuration (`ai_settings`, `ai_inflight`: bounded by user count or fixed keys). Today there is no account deletion, so a departed user's rows are never removed (gap G1).
- **Raw per-call data lives 30 days, aggregates up to 400 days, per-user scan answers 30 days at most, counters 90 days.** Aggregates carry no user ID.
- **On the phone, the photo is the only thing that is big.** A scan photo is kept until it is scanned and dismissed, or 7 days after it failed. The pending queue is capped at 50 photos; failed photos have an age limit and no count limit, and completed photos the user has not reviewed have no limit until the next app launch (G3). Everything else on the device is small and bounded.
- **Today the app stores the camera's full-size JPEG** (quality 0.7). The server shrinks every photo to 1,568 px on the long edge anyway. Recommendation: shrink once at capture to the same size (G2), which cuts the queue from about 125 MB to about 15 MB (estimates).
- **Plan limits are not in the repo**: Render Postgres is `basic-256mb` (`render.yaml:244`); Supabase's plan and size are not recorded. Confirm both before the 1,000-user mark (see [growth](#expected-growth)). The dominant table is `scan_records` (about 450 KB per daily-active user at steady state, estimate).

---

## On the phone

### What is stored and how big (estimates)

| Item | Where | What it holds | Size per item (*estimate*) | Cap |
|---|---|---|---|---|
| Scan photo | document directory `scan-queue/<ms>-<seq>.jpg` (`frontend/lib/scanJobStorage.ts:8`; written at `frontend/app/(tabs)/scan.tsx:150-159`) | the camera's JPEG as captured, **not resized**, `quality: 0.7` (`scan.tsx:112`) | 12 MP phone: about 1.5 to 3 MB, call it 2.5 MB. 48 to 50 MP phone: about 5 to 10 MB. See the note below | 50 waiting; none for failed (see caps) |
| Camera original | the app cache directory (the `photo.uri` that `takePictureAsync` returns) | the same JPEG; `scan.tsx:159` copies it and nothing deletes the source | same as above, again | none in the app; the OS may clear the cache under pressure. Until then each capture costs about twice its size |
| Job metadata | AsyncStorage key `scan_job_queue` (`scanJobStorage.ts:7,29-35`) | the `ScanJob` list as one JSON string; `results` are stripped before saving | about 250 to 400 bytes per job | follows the job count (below) |
| Auth tokens | SecureStore keys `bookshelf_access_token`, `bookshelf_refresh_token` (`frontend/lib/api.ts:8-9`, `frontend/lib/storage.ts`) | two RS256 JWTs | about 0.5 to 1 KB each | two values |
| Install marker | AsyncStorage key `bookshelf_install_marker` (`frontend/lib/freshInstall.ts`) | `"1"` (done) or `"pending"` (a wipe is owed: a fresh-install wipe that did not finish, a launch whose check could not read AsyncStorage, or a sign-out whose delete failed; the next launch wipes first, as for any other value). Absent with no other AsyncStorage keys means a fresh install, so Keychain tokens left by a previous install are deleted; absent with other keys means an upgrade, so `"1"` is written and the tokens kept (#722) | a few bytes | one value |
| Session log buffer | memory only (`frontend/components/FeedbackWidget/SessionLogger.ts`) | the last 200 `console.warn` / `console.error` lines | each line cut at 1,000 characters (`…[truncated]` marker); `getLogs()` capped at 100 KB (UTF-8) | 200 entries, gone when the app closes |
| Book covers | the OS image cache for `<Image>` (`app/(tabs)/my-books.tsx`, `wishlist.tsx`, `components/BookCandidatePicker.tsx`) | remote cover thumbnails | tens of KB each | none set by the repo; the OS manages it |
| Sentry | native SDK offline cache | queued error events | not configured by the repo | SDK default; plan limit to confirm |

Notes on the photo size:

- JPEG size depends on the sensor resolution and the scene, and `expo-camera`'s `quality` is the only knob the app turns. These figures are rules of thumb for a busy bookshelf at 0.7 and **need a device measurement** (story G2 includes taking one).
- The server accepts at most 5 MB per image (`backend/app/api/scan.py:53`, `413` above it) and the app has no handling for that code (`frontend/lib/scanErrors.ts` has no 413 case, so it becomes the `generic` state). A 48 to 50 MP capture at 0.7 may exceed 5 MB and fail on every retry. This is the strongest argument for resizing on the phone.
- The document directory is included in iOS device and iCloud backups. Failed and queued photos therefore also count against the user's backup.

### Caps today

| Cap | Value | Where | What it limits |
|---|---|---|---|
| `MAX_QUEUE_SIZE` | 50 | `frontend/lib/scanJob.ts:7`; enforced `ScanJobContext.tsx:451` (`isQueueFull`) and the backstop `:459-461` | jobs with status `queued` or `pending` (`isPendingUpload`, `scanJob.ts:60`). Does not count `searching`, `complete` or `failed` jobs, nor text searches' (zero) photos. The comment at `scanJob.ts:3-6` says so |
| `FAILED_SCAN_RETENTION_MS` | 7 days | `scanJob.ts:10`, `isFailedScanExpired` `:111` | age of a `failed` job, counted from `failedAt`, or `createdAt` for jobs saved before #605 (`failedTimeOf` `:106`) |
| `SCAN_RETRY_DELAYS_MS` | 30 s, 10 min, 1 h, 4 h | `frontend/lib/scanErrors.ts:130-135` | waits before attempts 2 to 5 of a photo after `503 scan_unavailable` (#604) |
| `MAX_SCAN_ATTEMPTS` | 5 | `scanErrors.ts:145` | attempts per photo; after the 5th the job is `failed` (`ScanJobContext.tsx:1006-1012`). Total wait on the schedule: 4 h 40 min 30 s |
| `MAX_SCAN_RETRY_DELAY_MS` | 4 h | `scanErrors.ts:142` | no retry is scheduled further ahead than this; also applied to saved times on launch (`ScanJobContext.tsx:240`) |
| `MAX_RETRIES` | 3 | `ScanJobContext.tsx:43`, used `:532` | manual "Retry" presses per job; the 4th puts the job back to `queued` with the banner `maxRetries` |
| `MAX_IN_PROGRESS_RETRIES` | 3 | `scanErrors.ts:99` | silent retries of `429 scan_in_progress` |
| `MAX_DRAIN_PASSES` | 3 | `ScanJobContext.tsx:46` | passes of the upload drain per trigger |
| `MAX_ENTRIES` (log buffer) | 200 | `SessionLogger.ts` | lines kept for a bug report |
| `MAX_LINE_CHARS` / `MAX_LOG_BYTES` (log buffer) | 1,000 characters / 100 KB | `SessionLogger.ts` | one stored line / the whole `session-logs.txt` attachment (oldest lines dropped first) |
| `FEEDBACK_RATE_LIMIT_MAX` / `FEEDBACK_RATE_LIMIT_WINDOW_MS` (feedback) | 5 submissions per 10 min, per install (module memory, reset on app restart) | `components/FeedbackWidget/useFeedbackSubmit.ts:34-35` | rate of feedback submissions; limits how often logs are sent, not their size |
| Server image cap | 5 MB | `backend/app/api/scan.py:53` | one upload; larger gets `413 Image must be 5MB or smaller` |

There is **no total-bytes cap** and no free-space check anywhere in `frontend/` (no `getFreeDiskStorage`, no handling of a full disk).

Worst cases (*estimates*, 2.5 MB photos): 50 waiting photos is 125 MB; with 5 to 10 MB photos it is 250 to 500 MB. Failed photos add `captures per day x 7 x size`: a user who captures 20 photos a day through a week-long outage holds 140 photos, about 350 MB. A failed job needs a free pending slot to have been captured, so the two numbers add rather than multiply.

### When photos are deleted

| Rule | What happens | Where |
|---|---|---|
| Scan succeeded and the user closes the review | the job is dismissed and its image deleted | `ScanJobContext.tsx` `dismissReview` calls `dismissJob`; the delete is at `:649` |
| User deletes a job from the queue screen | job removed, image deleted (confirm text `queueDeleteConfirm`) | `frontend/app/scan-queue.tsx:60`, `dismissJob` `ScanJobContext.tsx:649` |
| Repeat of a photo whose books are all in the library | job and image dropped at once | `ScanJobContext.tsx:1252` |
| Queue is already full at `startScan` | the just-captured image is deleted | `ScanJobContext.tsx:459-461`. (The scan screen checks `isQueueFull` first, `scan.tsx:75,94,190`, so a rejected capture leaves no file) |
| Failed for 7 days | job and image removed on app launch (job dropped, file removed by the orphan sweep) and when the queue screen opens | `ScanJobContext.tsx:253-256` (launch), `:609-626` (`purgeExpiredFailedJobs`), called from `scan-queue.tsx:40` |
| Orphan photo (crash between copy and job save) | any file in `scan-queue/` no job refers to is deleted on launch; files stamped at or after the launch time are spared | `scanJobStorage.ts:85-111`, called `ScanJobContext.tsx:256` |
| Job was `complete` when the app was killed | dropped on launch (results are not persisted); its photo is swept | `ScanJobContext.tsx:243-256` |
| Job restored as `searching` | reset to `pending` and uploaded again | `ScanJobContext.tsx:246` |

Missing:

- **No deletion on low storage**, and no way to know storage is low.
- **No deletion after N days for `queued` / `pending` photos.** A photo that never reaches the server (the user stays offline or blocked by a limit) stays on the phone until it eventually fails or is dismissed. After 5 attempts it becomes `failed` and the 7-day clock starts, but a photo refused by a limit stays `queued` (`hasRefusedJobs`, `scanErrors.ts:93-96`) with no age limit.
- **No deletion of the camera original** in the cache directory.
- **Cleared on sign-out (#723):** when the user signs out (Settings button or a revoked session), `ScanJobContext` empties the in-memory queue, removes `scan_job_queue`, deletes the queued photos and sweeps `scan-queue/`, and a send still in flight drops its result. The next account therefore starts with an empty queue. Remaining bounding work (caps, sizes) is tracked in #669.
- **No per-photo size check** before queueing (a 5 MB+ photo is queued, then fails on every attempt).
- **No count cap on failed jobs.**
- **No cap on completed but unreviewed photos.** A successful scan keeps its image so the review sheet can offer an enhanced re-scan (`ScanJobContext.tsx:1261-1263`); it is deleted only when the user dismisses the review or on the next app launch. `isPendingUpload` (`scanJob.ts:60`) excludes completed jobs, so they do not count toward the 50-photo cap (`ScanJobContext.tsx:459`). A user who keeps scanning without opening results accumulates them without limit until the app restarts.

### Should photos be downscaled before queueing?

Yes. Recommendation: resize on the phone, once, at capture, before the copy to `scan-queue/`: long edge 1,568 px (the same value the server uses, `backend/app/ai/registry.py:126,137`), JPEG quality 0.8.

| | Today (full size, q 0.7) | Resized (1,568 px, q 0.8) |
|---|---|---|
| Photo size (*estimate*) | 1.5 to 3 MB (12 MP); 5 to 10 MB (48 MP) | about 250 to 450 KB at about 1.8 MP |
| 50 waiting photos | about 125 MB (250 to 500 MB on 48 MP) | about 15 to 20 MB |
| Upload on mobile data | 1.5 to 10 MB per scan | under 0.5 MB |
| Can exceed the 5 MB server cap | yes, on high-resolution sensors | no |
| What the model sees | the server shrinks to 1,568 px, JPEG q 85 (`backend/app/ai/images.py:5,20`) | the same pixels; no loss of what the model could use |

Trade-offs:

- **Quality loss is small but real:** a second JPEG generation (phone at 0.8, then the server's q 85). Spine text is read at 1,568 px either way. It removes any chance of the server later raising `max_image_long_edge_px` and benefiting from more pixels; if that is planned, store 2,048 px instead (about 1.5x the size).
- **Repeat detection is unaffected:** the fingerprint is the SHA-256 of the server-normalised bytes (`backend/app/ai/scan_records.py` Lifetime section), and a retry resends the same stored file, so the same photo still matches itself. A photo retaken is a different photo either way.
- **Cost:** a new native dependency (`expo-image-manipulator`) needs a native rebuild and the Podfile and Gradle rules in `CLAUDE.md` (rules 10, 16, 20). A cheaper partial step is lowering `quality` to 0.5, which saves only about a third and does not help with 48 MP.
- Do not delete the camera original later than the copy; with the resize, write the resized file and delete the original in the same step.

### What the user sees when a limit is hit

| Situation | What the user sees | i18n key (`scan` namespace, `frontend/src/i18n/locales/*/scan.json`) |
|---|---|---|
| Queue full (50 waiting) | red banner for 4 s; capture is refused (`scan.tsx:75,94,190`, `ScanJobContext.tsx:459-465`) | `queueFull`; `queueFullLimited` when a limit is what holds the queue ("You've reached your scan limit. Your saved photos will be sent when scanning is available again.") |
| Attempts used up (5) | job `failed`; "This scan failed. Tap Retry to try again." with "View queue" | `scanFailedFinal`, `scanFailedFinalRetry`, `scanFailedFinalViewQueue`, `viewQueueFailed` |
| 7-day retention | the queue screen shows "Failed scans are removed after 7 days." | `queueFailedNote` (`scan-queue.tsx:149`) |
| Delete a job | confirm dialog "Delete this scan? The photo will be removed." | `queueDeleteConfirm`, `queueDeleteCancel` |
| Manual retries used up (3) | "Maximum retries reached. Saved for later." | `maxRetries` |
| Photo cannot be saved (disk full, copy fails) | the generic "Something went wrong. Please try again." with Retry; Sentry gets stage `file_copy` | `scanFailedMessage` (`scan.tsx:160-181`) |
| Server `413` (over 5 MB) | the generic failure | `scanFailedTitle`, `scanFailedMessage` (no specific copy) |
| Free storage low | nothing | none (gap G5) |

### Logs and caches on the device

- **Session log buffer** (#523): memory only, 200 lines, sent as an attachment `session-logs.txt` to Sentry only when the user files a bug report (`useFeedbackSubmit.ts:76-90`). Each line goes through a bounded pipeline (G6, fixed by #671 and #758): (1) serialise each argument with a budget: the serialiser stops walking once about 4,000 characters exist and arrays are capped at 100 items, so a huge array or object costs the same as a small one (the engine still enumerates the keys of a many-key object once); (2) join the arguments and cut the text to a 4,000-character work size; (3) redact that text; (4) wherever a clip happened (a long argument, the joined text) drop a trailing run of token characters that looks like a fragment (contains a digit or one of `. @ % + / = -`, or starts `eyJ`) and add `…[truncated]`; (5) cut to 1,000 characters, again dropping a split fragment. A stored line is therefore at most 1,012 characters (1,000 plus the 12-character `…[truncated]` marker), not counting the timestamp and level prefix. Redaction therefore runs before the 1,000-character cut, so a secret that straddles it is already replaced. `getLogs()` returns at most 100 KB of UTF-8 (a lone surrogate counts as 3 bytes), dropping the oldest lines first.
  - Rules: `Authorization` and `Proxy-Authorization`: an unquoted value, whatever its scheme (Bearer, Basic, Digest, AWS4-HMAC-SHA256, custom), is redacted to the end of the line or to `&` in a URL query, giving `Authorization: [redacted]`; a quoted or JSON value is replaced inside its quotes; `Bearer <8+ token characters>` (also quoted, or after `bearer:`) becomes `Bearer [redacted]`, keyword case kept; `token`, `secret`, `secret_key`, `password`, `passwd`, `api_key`/`apikey`/`api-key` as the end of any key name (`client_secret`, `authToken`, `db_password`), followed by `=` or `:` (plain, quoted or JSON), have their value replaced by `[redacted]`; JWT-shaped strings (`eyJ` header, three base64url segments, also after `-` or `_`, also two glued together) and leftover `eyJ` runs of 10+ characters become `[jwt]`; email addresses (RFC 5322 local-part characters, a `key=` prefix is kept) become `[email]`.
  - Linear time: the keyed rules start only at a fixed keyword and use disjoint character classes; the JWT and email scans are single manual passes (a pure regex such as `[a-z]+@` is quadratic on `'a'.repeat(n)`); the unit test measures 16,000 against 4,000 characters over 22 pathological inputs.
  - Serialising never throws (circular objects, BigInt and throwing getters give `[unserializable]`).
  - Known limits: non-ASCII email addresses and dotless hosts (`user@localhost`) are not matched; a `key=` prefix on an email is kept, so `jane=doe@x.com` leaks `jane=`; JWTs whose header does not start `eyJ` and opaque secrets in free text with no recognised keyword are not detected; a keyed value only counts when the key ends with one of the words above (`sessionid=`, `cookie:` and `private_key` are not covered); JSON that was stringified a second time (`\"password\":\"x\"`) is not matched; a word such as `token: expired` is redacted even when it is not a secret (over-redaction is preferred); a secret longer than the 4,000-character work size is cut, and the cut fragment is dropped only when it looks like a token; a many-key object still costs one key enumeration by the engine (about 100 ms for 300,000 keys), and objects with many `undefined` or function-valued keys are not budget-limited (they emit nothing, about 275 ms for 300,000 keys); an unquoted keyed value ends at whitespace, `,`, `;`, `&`, `}` or a quote, so the rest of such a secret is stored (`password=hunter 2` becomes `password=[redacted] 2`); percent-encoded emails (`a%40b.com`) are not matched; a clip inside a Bearer token can leave up to 7 characters of it (a shorter value is not recognised as a token).
- **Sentry** (`frontend/lib/sentry.ts:22-28`): `tracesSampleRate` 0.2 in production, 1.0 elsewhere, `sendDefaultPii: false`. Offline cache and retention are SDK defaults.
- **AsyncStorage** holds only the job list. On Android the library's default database limit is 6 MB (library default, to confirm for the pinned version); the job list needs about 0.1 MB for 300 jobs, so there is no risk today.
- **SecureStore** holds only the two tokens (`frontend/lib/storage.ts`); web uses an in-memory map. Expo has warned about values over 2,048 bytes; the tokens are well under that (estimate, to confirm).

### Per-device budget

| | Typical | Worst case | Target after G2 / G3 |
|---|---|---|---|
| Waiting photos (50 cap) | 5 x 2.5 = 12 MB | 125 MB (500 MB on 48 MP) | 50 x 0.4 = 20 MB |
| Failed photos | 0 | about 350 MB (20 a day for 7 days) | cap 20 photos = 8 MB |
| Completed, not yet reviewed | 0 to a few | uncapped until the next app launch (each scan done without opening its results) | cap 10 photos = 4 MB (oldest unreviewed is dismissed first) |
| Camera originals in cache | one per capture until the OS clears it | same as the photos | 0 (deleted at capture) |
| Job list (AsyncStorage) | under 10 KB | about 0.1 MB | same |
| Tokens, log buffer, covers | under 1 MB combined | a few MB of covers | same |
| **Budget to adopt** | | | **at most 50 MB of scan photos in total, at most 80 photos (50 waiting + 20 failed + 10 completed but unreviewed)** |

---

## In the database

### Table-by-table retention

Purges are called from `cleanup_expired_tokens` (`backend/app/services/token_cleanup.py:46-115`): once 60 s after startup, then every 24 h (`:43-44`), each purge in its own `try` so one failure does not skip the rest. Each batched purge deletes 5,000 rows per batch and at most 20 batches per run, so at most 100,000 rows per table per run. The trash purge (#683) is smaller: 500 rows per batch, at most 20 batches (10,000 library rows) per run. "Matches doc" compares code with the table's section in [`data-model.md`](data-model.md).

| Table | Holds | Retention rule (code) | Purge | Matches doc |
|---|---|---|---|---|
| `users` (`models/user.py`) | one row per signed-in person | none; kept until deleted. No delete endpoint exists | none | yes (doc is silent on retention) |
| `books` (`book.py`) | shared catalog of works, written from signed provider data | none by design | none | yes |
| `editions` (`edition.py`) | shared catalog of editions | none by design | none | yes |
| `user_books` (`user_book.py`) | the user's library rows, live and in the trash (`deleted_at`, #683) | live rows: none; cascade with the user; no per-user cap. Trashed rows (`DELETE /user-books/{id}`): kept 30 days (`TRASH_RETENTION_DAYS`, `trash_retention_days` in `config.py`), one trashed copy per book (copies with your data are kept), and the newest 1,000 per user (`TRASH_MAX_ENTRIES_PER_USER`, 100–5,000), whichever comes first; restorable until then. A `DELETE` hard-deletes, in the same transaction, the user's other, older trashed rows of the same book that hold no user data (`trash.evict_same_book`, `UserBook.has_no_user_data()`: no notes, rating, edition, overrides, status beyond wishlisted or status dates; same work for a catalog entry, merged rows included; same title and author or ISBN for an unverified one), so an add / delete loop never grows the trash, while a copy with the user's data stays until the purge or the cap; then, as a backstop, the oldest trashed rows beyond the cap (`trash.evict_over_cap`), so a mass delete of up to 1,000 distinct books stays restorable. Worst case ~13 KB per trashed row (10,000 characters of notes plus overrides of 500 / 500 / 2,048, measured with `pg_column_size`), up to ~25 KB with multibyte notes: ~13–25 MB per user at the default cap, ~65–125 MB at the maximum of 5,000 | `app/services/trash.py:purge_trash`, called from `token_cleanup.py:_purge_trash`; batches of 500 rows, at most 20 per run (10,000 rows), one commit per batch, `SKIP LOCKED`, under the advisory lock `pg_try_advisory_xact_lock` so two instances never purge at once (#683) | yes (#683 updated both) |
| `user_custom_books` (`user_custom_book.py`) | the user's own unverified entries | cap 200 live entries per user (`config.py`, `unverified_books_per_user_max`; trashed ones do not count) plus at most `TRASH_MAX_ENTRIES_PER_USER` (1,000) trashed ones, so at most 1,200 rows per user (~1.3 MB worst case); cascade with the user and with their library row; trashed, restored and evicted with their library row | purged with their library row by the trash purge, the same-book eviction and the per-user trash cap (#683) | yes |
| `refresh_tokens` (`refresh_token.py`) | one row per issued refresh JWT | expired rows (`expires_at < now`, 7 days after issue, `refresh_token_expiry_days`) and rows revoked without rotation (logout, logout-all); rotated rows stay until they expire, for reuse detection (#690) | `app/auth/refresh_family.py:purge_refresh_tokens`, called from `token_cleanup.py:_purge_refresh_tokens`; batches of 5,000, at most 20 per run, `SKIP LOCKED` (#690) | yes (#690 updated both) |
| `ai_quota` (`ai_quota.py`) | per-user, per-day counters (`scan`, `scan.no_match`, `scan.requests`) and legacy month rows | day rows 90 days (`DAILY_RETENTION_DAYS` `app/ai/quota.py:235`); month rows 13 months (`:236`) | `quota.purge_old_quota` `:1695`, `_purge_kind` `:1653` | yes |
| `ai_inflight` (`ai_quota.py`) | one lease row per user with a call in flight | deleted on release; a crashed request's row is replaced at that user's next scan | none; bounded by user count | yes |
| `ai_settings` (`ai_settings.py`) | operator dials | fixed set of keys | none; does not grow | yes |
| `ai_usage` (`ai_usage.py`) | one row per provider call, with user ID, tokens and cost | 30 days raw (`RAW_RETENTION_DAYS` `app/ai/ledger.py:74`), then rolled up into `ai_usage_daily` and deleted in one transaction | `ledger.rollup_and_purge` `:326-348`, `_rollup_batch` | yes |
| `ai_usage_daily` | per (day, feature, model, status) totals, no user | 400 days (`DAILY_RETENTION_DAYS` `ledger.py:75`) | `_purge_daily` `ledger.py:256` | yes |
| `ai_spend_daily` (`ai_spend.py`) | one row per UTC day: reserved and settled micro-dollars, alert mask | 90 days (`RETENTION_DAYS` `app/ai/breaker.py:112`) | `breaker.purge_old_spend` `:327` | yes |
| `ai_enhanced_zero_results` | uncharged zero-result enhanced scans per user | 30 days (`ZERO_RESULT_RETENTION_DAYS` `quota.py:240`) | `quota.purge_old_zero_results` `:1703` | yes |
| `scan_records` (`scan_record.py`) | per-user answer for a photo: SHA-256, candidates, enrichment JSON | `scan_records.retention_days`, default 30, bounds 1 to 30 (`app/ai/settings.py:210,213`); also at most `scan_records.max_per_user`, default 500, bounds 1 to 10,000 (`:211,214`), evicted oldest first on save | `scan_records.purge_old_scan_records` `:495`, `purge_old` `:473`. If settings are unreadable it uses 30 | yes |
| `scan_corrections` (`scan_correction.py`) | which of title / author / ISBN a user edited on a pre-filled save (#631) | 30 days (`ledger.py` uses `RAW_RETENTION_DAYS`); also cascades with its `scan_records` row | `ledger._purge_scan_corrections` `:287`, called inside `rollup_and_purge` `:344` | yes |
| `scan_corrections_daily` | per (day, feature, model) counts, no user | 400 days (`ledger.py:75`) | the same function, `:315-317` | yes |

Findings from checking each row against the code:

1. Every retention number in the issue's list and in `data-model.md` matches the code. The docstring of `token_cleanup.py:1-27` also matches.
2. **`refresh_tokens` was the only table with no retention text in `data-model.md`.** Added in this change.
3. **Nothing purges `users`, `books`, `editions`, or live `user_books` / `user_custom_books` rows** (since #683 the trash is purged 30 days after deletion), and there is no account-deletion endpoint (`backend/app/api/` has only `auth`, `books`, `scan`, `user_books`). `data-model.md` notes "Phase 2 account deletion ... not built yet" in several places. Every per-user table above cascades from `users`, so a deletion endpoint is one `DELETE` on `users` plus the `ai_usage` rows, which use `ON DELETE SET NULL` (`models/ai_usage.py:83-86`) and keep their cost data without a user. Account deletion (#291) must remove the user's trashed library rows at once too (the cascade does), not leave them for the 30-day trash purge.
4. The `scan_corrections` purge is wrapped so a failure does not fail the usage rollup (`ledger.py:344-357`); the other purges are isolated by their own `try` in `token_cleanup.py`.
5. `ai_usage` has no per-user cap and no per-user row limit apart from the request limits (`scan.requests_per_user_daily` 100, `docs/scan-limits.md`), so a user can add at most about 100 rows a day.
6. The 100,000 rows per table per run ceiling is far above the daily rate even at 10,000 users (below). If a purge ever falls behind (the service was down for days), it catches up at 100,000 rows a day per table.
7. The task runs inside the API process (`app/main.py:238`). Two instances both run it; every purge is idempotent, so that is safe.

### Expected growth

Assumptions (state them again when you change them):

- An **active user** is active every day (an upper bound for the daily-active count; monthly-active users are cheaper). Each makes **3 scans a day**, all new photos, **1 provider call each**, no enhanced scans, one correction per 10 days.
- Library: **1 new `user_books` row and 0.3 new shared `books` rows a day** per active user (the catalog saturates as users overlap).
- Row sizes are those in `data-model.md`, plus about 30 percent for row headers and indexes. `scan_records` is **5 KB** typical (the doc gives 1 to 10 KB, 80 KB for a full shelf), which is the number most worth measuring on production.

Per active user per day (*estimate*):

| Table | Rows | Bytes |
|---|---|---|
| `scan_records` | 3 | 15,000 |
| `ai_usage` | 3 | 240 |
| `ai_quota` | 3 | 210 |
| `refresh_tokens` | 4 (one per 15 minutes of use, about an hour a day; #689) | 600 |
| `scan_corrections`, `ai_enhanced_zero_results` | under 0.1 | under 10 |
| `user_books` + `books` | 1.3 | 850 |
| **Total written** | **about 14** | **about 17 KB** |

Steady-state size (what the purges allow) and yearly growth (what they do not):

| Active users | Rows written per day | Steady-state purgeable (scan_records 30 d, usage 30 d, quota 90 d, tokens 7 d) | Rows in `scan_records` | Rows in `ai_quota` | Library + catalog growth per year |
|---|---|---|---|---|---|
| 100 | 1,400 | about 50 MB | 9,000 | 27,000 | about 30 MB |
| 1,000 | 14,000 | about 500 MB | 90,000 | 270,000 | about 300 MB |
| 10,000 | 140,000 | about 5 GB | 900,000 | 2,700,000 | about 3 GB |

(Steady state per user is `90 x 5 KB` = 450 KB of scan records, 7 KB of usage, 19 KB of quota and about 5 KB of tokens; rounded.) `scan_records` is about 90 percent of it. With a full-shelf 80 KB record the same user would be 7 MB. The per-user cap of 500 bounds the worst case at 2.5 MB (5 KB records) to 40 MB (80 KB records) per user; the setting can go up to 10,000 records.

Aggregate tables are negligible: `ai_usage_daily` and `scan_corrections_daily` are tens of rows a day (under 1 MB over 400 days), `ai_spend_daily` is 90 rows.

Plan limits:

| Environment | Database | Plan in the repo | Storage limit |
|---|---|---|---|
| Production (`bookshelf-api`) | Supabase Postgres, session pooler (`render.yaml:1-9`, `docs/RENDER.md:44-62`) | the repo says to upgrade to Pro before store submission (`RENDER.md:61,187`); the current plan is not recorded | plan limit to confirm |
| Staging / dev (`bookshelf-api-dev`) | Render Postgres `bookshelf-db`, `basic-256mb` (`render.yaml:243-244`, `docs/deployment.md:122`) | `basic-256mb` | plan limit to confirm (no `diskSizeGB` in `render.yaml`) |

Reading the table: at 100 users the purgeable data (about 50 MB) is small against any paid plan. At 1,000 users it is about 500 MB, which is where the plan's limit must be known. At 10,000 users it is about 5 GB plus 3 GB a year of library growth. The backup job dumps the whole database daily and keeps 30 dumps (`docs/backups.md:187-195`): at 5 GB uncompressed that is up to 30 x the compressed size in R2's 10 GB free tier, so the backup retention needs a look at the same time (G8).

### Logs

What the backend logs (all structured JSON to stdout, `backend/app/core/logging.py`; redaction in `security.md#logging`):

- **Per request:** the uvicorn access line (method, path, status) and a request ID set by `RequestIdMiddleware` (`app/main.py:57`). Rate-limit hits log `rate_limit_exceeded path=...` (`main.py:257`).
- **Per sign-in:** a handful of `info` lines (`app/api/auth.py:35-85`), the last with the internal user ID, never the email.
- **Per scan:** `image normalised: WxH, N bytes -> M bytes` (`app/ai/images.py:174`), one `scan_record event=... books=... in_library=...` line with counts only (`app/ai/scan_records.py:541`), retry lines from the provider adapter (`anthropic_vision.py:600`), and `warning` lines for ceilings and non-fatal failures (`app/api/scan.py:178-443`). No image, filename, raw model output or token.
- **Per day:** one line per purge from `token_cleanup.py`.

Where they go:

- **Render logs** (stdout of `bookshelf-api` and `bookshelf-api-dev`). Retention is set by the Render workspace plan, not by the repo: plan limit to confirm.
- **Sentry**: backend errors and 20 percent of traces when hardened (`app/main.py:47`), scrubbed by `app/core/sentry_context.py`; the app sends errors and 20 percent of traces in production. Retention is set by the Sentry plan: plan limit to confirm.

Retention the repo controls: none. It controls only what is logged (the rules above) and the sampling rate. There is no log file, no log table and no `logging` handler that writes to disk or the database.

### Gaps and follow-ups

| # | Gap | Where |
|---|---|---|
| G1 | No account deletion; `users`, `user_books`, `user_custom_books`, `ai_*` per-user rows and `refresh_tokens` of a departed user stay forever | `backend/app/api/` |
| G2 | Photos are stored full size; the server cap (5 MB) can be exceeded on high-resolution phones | `scan.tsx:112,159` |
| G3 | Failed jobs have no count cap; refused (`queued`) jobs have no age cap; completed-but-unreviewed jobs keep their photo, uncapped, until the next launch | `scanJob.ts:7-10,60`, `ScanJobContext.tsx:1261-1263` |
| G4 | The camera original in the cache is never deleted; queued photos are not deleted on sign-out | `scan.tsx:159`, `AuthContext` |
| G5 | No free-space check and no storage-full message | `frontend/` |
| G6 | ~~Session log lines are unbounded in length~~ Fixed by #671/#758: 1,000-character lines, 100 KB attachment, bounded serialisation, redact-then-cut; known redaction limits are listed under "Logs and caches on the device" | `SessionLogger.ts` |
| G7 | ~~`refresh_tokens` purge is one unbatched `DELETE`~~ Fixed by #690: batched like the others; doc text added | `app/auth/refresh_family.py:purge_refresh_tokens` |
| G8 | `scan_records` dominates the database and `docs/backups.md` sizes backups on the dump size; neither production size nor plan limit is recorded | `docs/RENDER.md`, `docs/backups.md` |
| G9 | Log and Sentry retention are not documented anywhere | n/a |

#### Proposed follow-up stories

Sizes: S under half a day, M about a day, L several days.

**1. Shrink scan photos at capture (G2) - M**
- Why: the queue holds full-size JPEGs (about 2.5 MB; 5 to 10 MB on 48 MP phones) although the server uses 1,568 px, and uploads over 5 MB are refused with an unhandled `413`.
- AC: a capture is resized to a 1,568 px long edge at JPEG 0.8 before it is saved to `scan-queue/`; the camera original is deleted in the same step; the size constants live in one place with a comment pointing to `max_image_long_edge_px`; a device measurement of before and after sizes goes in the PR; the Podfile.lock / Android prebuild rules of `CLAUDE.md` are followed; a 413 gets its own state and copy in `scanErrors.ts` (all 13 locales); jest tests cover the resize call and the deletion.

**2. Bound the failed, refused and unreviewed queue (G3, G4) - S**
- Why: failed photos have only an age limit, `queued` photos refused by a limit have no limit at all, and completed photos the user has not reviewed are kept until the next launch, so a long outage, a blocked account or a run of scans without opening results fills the phone.
- AC: at most 20 failed jobs are kept (the oldest is removed with its photo when a 21st fails); a `queued` job older than 7 days is failed or removed with its photo, with copy in `queueFailedNote` style; at most 10 completed-but-unreviewed jobs keep their photo (the oldest is dismissed, photo deleted, when an 11th completes); queued and failed photos and the job list are cleared on sign-out; tests extend `scanJob` / `ScanJobContext` / `scanJobStorage` tests; docs updated here and in `frontend.md`.

**3. Check free space before queueing (G5) - S**
- Why: a full disk shows only "Something went wrong" and a Sentry event.
- AC: below a free-space threshold (for example 100 MB, via `expo-file-system`) a capture is refused with a specific message; new i18n keys in all locales and the key-parity files; the total scan-queue size is capped at 50 MB (this page's budget), counted before each save; tests with a mocked free-space value.

**4. Account deletion and data export (G1) - L**
- Why: no table of a departed user is ever purged, `data-model.md` says "Phase 2 account deletion ... must include" six tables, and the app stores account data (stores require in-app deletion where an account can be created; confirm the current store rule).
- AC: `DELETE /auth/me` (rate limited with `rate_limit_writes`) removes the user and, by cascade, all per-user rows; `ai_usage` rows keep their cost with `user_id` NULL; refresh tokens revoked; a test per table (using the test added with this page to list them); the app has a confirm step and signs out; privacy page updated. Needs an owner decision on export. Check first whether an issue already exists.

**5. Truncate session log lines (G6) - S** (done in #671/#758)
- Why: the 200-line buffer is attached to Sentry, and one line can be arbitrarily large.
- AC: each line is cut at 1,000 characters with a marker; total attachment at most 100 KB; unit tests in `SessionLogger` tests.

**6. Batch the refresh-token purge (G7) - S** (done in #690)
- Why: it is the one purge that is not bounded; at 10,000 users after an outage it could delete hundreds of thousands of rows in one transaction.
- AC: `cleanup_expired_tokens` deletes in batches of 5,000 (20 per run) like the others; an integration test for the batching; `data-model.md` text unchanged.

**7. Record production size and plan limits (G8, G9) - S**
- Why: the plan and size limits for Supabase and Render Postgres, and Render and Sentry log retention, are not in the repo, so nothing says when to upgrade.
- AC: `docs/RENDER.md` records the plan and limit of each database and log retention of Render and Sentry; a query (documented, read-only) measures size per table and the average `scan_records` row; this page's growth table is re-run with the measured numbers; a size alert is set at 70 percent of the limit; the backup lifecycle in `docs/backups.md` is checked against the measured dump size.

**8. Decide whether `scan_records` should store less (G8) - M, owner decision**
- Why: it is about 90 percent of the data. The enrichment holds book descriptions of up to 5,000 characters that the catalog already has.
- AC: an owner decision recorded on the issue; if "yes", the record stores only IDs and the response is rebuilt from the catalog, a migration compacts existing rows, `data-model.md` updated. Not started until the measurement in story 7 shows it matters.

## Keeping this page right

`backend/tests/unit/test_storage_retention_doc.py` fails when a table in `app/models` is not named in the retention table above, and when a documented retention constant no longer matches the code. A story that adds a table adds its row here and, if it grows with usage, its purge to `token_cleanup.py`.

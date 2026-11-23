# App Store and Play Store compliance audit

**Issue:** #695 (part of #681) · **Audited:** 2026-11-22 against `origin/dev` at `7296760`
· **Related:** [`claude/ios-appstore-submission.md`](claude/ios-appstore-submission.md),
[`claude/ios-ci.md`](claude/ios-ci.md), [`claude/android-ci.md`](claude/android-ci.md),
[`privacy.html`](privacy.html), [`storage-and-retention.md`](storage-and-retention.md)

This is a point-in-time audit written from the code and config in the repo plus the
reviewer's knowledge of the store guidelines. It is **not** a substitute for the live
App Store Connect and Play Console checks. Where a store requirement changes often
(Play's target API level, Apple's required-reason API list, Data safety wording) the row
says **verify against the current console** instead of quoting a number.

Status key: **pass** (met, evidence cited) · **partial** (some of it met) · **fail**
(not met) · **verify** (cannot be decided from the repo; owner checks the console) ·
**n/a** (does not apply). Every **fail** and **partial** row links an issue; **verify**
rows link one where an owner action exists.

## Summary of blockers

| Blocker | Store | Issue |
|---|---|---|
| No in-app account deletion | Apple 5.1.1(v), Play | #291 |
| No web deletion URL (Play requires one) | Play | #1014 |
| Google sign-in offered without Sign in with Apple | Apple 4.8 | #693 (secrets #679) |
| Release build signed with the debug keystore | Play | #893 |
| iOS privacy manifest declares no collected data | Apple | #1001 |
| Privacy policy omits Google sign-in, Sentry, AI usage records, deletion path, and wrongly claims a hashed password | Both | #1015 |
| No reviewer access path for the Google-only sign-in | Both | #1018 |

## Apple App Store

| Item | Requirement | Status | Evidence | Issue |
|---|---|---|---|---|
| 4.8 Login services | An app that offers a third-party or social login (Google) must also offer an equivalent privacy-preserving option (Sign in with Apple, or a different option meeting the 4.8 criteria). Google is the only production sign-in, so the rule applies. | **fail** | Google sign-in at `frontend/app/(auth)/login.tsx:3,37,117-127`; no Apple provider in `frontend/package.json`; backend only has `backend/app/auth/google.py` | #693 (secrets #679) |
| 5.1.1(v) Account deletion | If the app lets users create an account, it must let them start deletion inside the app. | **fail** | No account-deletion endpoint in `backend/app/` (only `DELETE /user-books/{id}` at `backend/app/api/user_books.py:400`); no UI. The privacy policy tells users to email (`docs/privacy.html:36-37`), which does not satisfy 5.1.1(v) | #291 |
| 5.1.1 Privacy policy link | Policy URL in App Store Connect metadata and reachable from inside the app. | **partial** | URL recorded as `https://wcmchenry3-stack.github.io/book_app/privacy.html` (`docs/claude/ios-appstore-submission.md:119,181`; repo has since been renamed, owner must confirm the Pages URL still resolves). In-app link: only from the AI consent prompt (`frontend/components/AiConsentPrompt.tsx`, URL in `frontend/lib/aiConsent.ts`, #1023); none in Settings yet | #1015 |
| 5.1.1 Policy accuracy | Policy must name what is collected and who receives it. | **fail** | `docs/privacy.html:16-20,26` says only account and reading data, and no analytics. Line 18 claims "a hashed password", which is false (Google is the only sign-in and `User` has no password column). `display_name` and `avatar_url` are stored (`backend/app/models/user.py:30-31`) but not disclosed. It does not mention Google sign-in data, Sentry crash reports, or AI usage records. It does disclose Anthropic and the 30-day scan retention (`:33-34`) | #1015 |
| Privacy nutrition label | Label in App Store Connect matches real collection. | **verify** | Proposed answers below, pending owner approval | #473 (store privacy answers doc) |
| 5.1.2(i) Third-party AI disclosure | Since the Nov 2025 guideline update, an app must disclose where personal data is shared with a third-party AI and get explicit permission first. Scan photos are sent to Anthropic. | **pass (prompt shipped; enforcement off until release)** | Before the first photo leaves the device the app shows "Scanning uses AI" (names Anthropic, what is kept and for how long, links the privacy policy; Continue / Not now): `frontend/components/AiConsentPrompt.tsx`, `frontend/contexts/AiConsentContext.tsx`, gate in `frontend/lib/scan/executeScan.ts`. Consent is recorded per account on the server (`users.ai_consent_at`, `POST /users/me/ai-consent`) and can be withdrawn in Settings. Server enforcement (`AI_CONSENT_REQUIRED`, `403 ai_consent_required` from `POST /scan`) is off until the app release with the prompt is out (rollout A); turn it on then | #1023 |
| Privacy manifest: collected data | `NSPrivacyCollectedDataTypes` lists the data types the app (and its SDKs) collect. | **fail** | Empty: `frontend/app.json:51`, `frontend/ios/BookShelfAI/PrivacyInfo.xcprivacy:43-44`. The app collects email, name, user ID, photos, crash data | #1001 (in progress; not edited here) |
| Privacy manifest: required-reason APIs | Every required-reason API category used by the app or a bundled SDK is declared with an approved reason. | **partial** | Four categories declared (FileTimestamp, UserDefaults, DiskSpace, SystemBootTime): `frontend/app.json:33-48`, mirrored in `PrivacyInfo.xcprivacy:5-42`. React Native, Expo and Sentry SDKs ship their own manifests; the app-level list was not diffed against the built archive. **Verify**: archive, then in Xcode Organizer select the archive, then Generate Privacy Report, and compare | #1016 |
| Privacy manifest: tracking | `NSPrivacyTracking` false and no tracking domains when nothing tracks. | **pass** | `app.json:52-53`, `PrivacyInfo.xcprivacy:45-48`; no ad or analytics SDK in `frontend/package.json` | None |
| ATT (App Tracking Transparency) | Only needed if the app tracks (shares data with data brokers or ad networks, reads the IDFA). | **pass** | No ATT dependency, no `NSUserTrackingUsageDescription` in `frontend/ios/BookShelfAI/Info.plist`, no ad SDK; policy says no advertising identifiers (`docs/privacy.html:26`). Re-check whenever an analytics or ad SDK is added | None |
| Purpose strings: camera | `NSCameraUsageDescription` present and specific. | **pass** | `frontend/ios/BookShelfAI/Info.plist:55-56`, from `frontend/app.json:74-77` | None |
| Purpose strings: unused permissions | Do not declare capabilities the app does not use; reviewers query unexplained strings. | **partial** | `NSMicrophoneUsageDescription` and `NSFaceIDUsageDescription` are generic template text (`Info.plist:57-60`), and nothing in `frontend/app` uses the microphone or Face ID | #1017 |
| Purpose strings: photo library | Needed only if the app reads the photo library. | **pass** | No `expo-image-picker` or media-library dependency in `frontend/package.json`; scanning uses the camera only. If gallery upload is added, add `NSPhotoLibraryUsageDescription` and revisit the label | None |
| Encryption export compliance | `ITSAppUsesNonExemptEncryption` set so builds are not held at "Missing Compliance". | **pass** | `false` at `frontend/app.json:20` and `Info.plist:42-43`. Valid only while the app uses HTTPS and OS crypto alone; owner confirms for the annual self-classification | None |
| Support URL | Working support URL in App Store Connect metadata. | **fail** | No support page in the repo or docs. Only a contact email in the policy (`docs/privacy.html:39-40`) | #1018 |
| Demo account for review | Reviewers need a working sign-in path with the shipped Google-only flow. | **fail** | No review access documented under `docs/`. The only non-Google login, `POST /auth/test-login` (`backend/app/main.py:477`), is registered only when not hardened and the server refuses to boot in staging or production with `TEST_AUTH_SECRET` set (`backend/app/core/config.py:460-468`), so it cannot be used for review. The only gate on production is the Google email allowlist (`ALLOWED_EMAILS`, `backend/app/api/auth.py:81`, `config.py:193`). A reviewer-access mechanism is a prerequisite: the realistic option is a dedicated Google test account whose email is on `ALLOWED_EMAILS` (if set) and whose credentials the owner shares in the review notes | #1018 |
| Age rating | Questionnaire answered; policy-consistent rating. | **verify** | Not recorded. Content is user-supplied book metadata plus AI-generated text from photos, no user-to-user content. Owner completes the questionnaire; note "AI generated content" if the console asks | #1018 |
| Sign-in without friction (5.1.1(v), 4.0) | Do not force sign-in for features that do not need it. | **verify** | Not assessed here; owner confirms the review flow reaches core value after sign-in | #1018 |
| Data export (privacy best practice, not a store rule) | Users can get a copy of their data. | **pass (ships off until the owner configures storage)** | Settings > "Export my data" (`frontend/components/AccountExportSetting.tsx`) calls `POST /account/export` (#694); a background job builds one JSON file of every user table (`backend/app/services/account_export.py`), stores it in a private Supabase Storage bucket and emails a link valid 7 days, after which the file is deleted. Answers 503 `export_not_configured` until `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` and `RESEND_API_KEY` are set (`docs/deployment.md`). Data access and portability (GDPR Art. 15 and 20, CCPA right to know): machine-readable JSON, `schema_version` 1 | #694 |
| Web / PWA | Web build is outside store review. | **n/a** | | None |

## Google Play

| Item | Requirement | Status | Evidence | Issue |
|---|---|---|---|---|
| Account deletion in app | Apps with account creation must offer in-app deletion. | **fail** | As above: no endpoint, no UI | #291 |
| Account deletion web URL | A web URL users can use to request deletion without the app, entered in the Data safety form; it must name the app and developer and state what is deleted and kept. | **fail** | No such page. `docs/privacy.html:36-37` is a policy paragraph with an email, not a dedicated deletion page or form | #1014 |
| Data safety form | Form matches real collection, sharing and security practices. | **verify** | Proposed answers below, pending owner approval | #473 |
| Privacy policy link | Public, non-geofenced URL in the Play listing and inside the app. | **partial** | Same URL and same missing in-app link as iOS; policy gaps as above | #1015 |
| Target API level | New apps and updates must target the API level Play currently requires. **Verify against the current console** (the requirement rises each year, usually by 31 August). | **verify** | `targetSdkVersion rootProject.ext.targetSdkVersion` (`frontend/android/app/build.gradle:96`) resolves to 36 per `docs/claude/android-ci.md:111` (Expo SDK 58 / RN 0.88.1); compileSdk 37 | #1019 (confirm in console, no code change unless Play requires higher) |
| Release signing | Upload key must be the app's own keystore; Play App Signing recommended. | **fail** | `release` build type uses `signingConfigs.debug` (`frontend/android/app/build.gradle:102-118`) with the public debug keystore | #893 |
| Permissions: declared set | Only permissions the app needs; sensitive ones need a declaration. | **partial** | Main manifest declares `CAMERA`, `INTERNET`, `READ/WRITE_EXTERNAL_STORAGE` (max SDK 32), `RECORD_AUDIO`, `SYSTEM_ALERT_WINDOW`, `VIBRATE` (`frontend/android/app/src/main/AndroidManifest.xml:2-8`). `app.json:64` lists only `CAMERA`. `RECORD_AUDIO` is unused by app code; `SYSTEM_ALERT_WINDOW` is a dev-tools permission that normally lives in the debug manifest (`src/debug/AndroidManifest.xml:4`) and should not ship in release | #1017 |
| Photos and videos permissions policy | Broad media permissions are restricted. | **pass** | Storage permissions capped at SDK 32 and no `READ_MEDIA_*` | None |
| Advertising ID | If the app uses the advertising ID, declare it; otherwise no `AD_ID` permission. | **pass** | No `AD_ID` in the manifest; no ad SDK | None |
| Content rating | IARC questionnaire completed. | **verify** | Not recorded | #1018 |
| Target audience and content | Not child-directed; Families policy does not apply. | **verify** | App is a general-audience book tracker; owner selects 13+ or 18+ per policy | #1018 |
| Demo / review access | Review instructions and working credentials if sign-in is needed. | **fail** | Not documented | #1018 |
| Encryption in transit | Data safety "encrypted in transit" answer: yes, HTTPS only | **pass** | `frontend/ios/BookShelfAI/Info.plist` ATS blocks arbitrary loads (`NSAllowsArbitraryLoads` false) | None |
| App integrity | Play Integrity usage is optional; relevant to Data safety (device identifiers). | **verify** | Attestation is wired on `/scan` but currently not required (`docs/security.md:25`). If enabled, Play Integrity tokens go to Google; mention in the answers | #1019 |

## Privacy label / Data safety answers

**Status: pending owner approval.** These are the reviewer's proposed answers from the code
and `docs/integrations.md` / `docs/storage-and-retention.md`. They must be reconciled with
the wording agreed in #549 and the store-answers doc from #473 (no such doc exists under
`docs/` yet; the release plan only lists it as planned, `docs/RELEASE-PLAN-2026-10.md:76`).
Do not paste into either console until the owner approves, and update this section
whenever the app starts collecting something new.

Facts the answers rest on:

- Photos of shelves or covers go to our backend, which strips metadata, re-encodes, and
  sends the processed image to Anthropic. We keep only a fingerprint of the image and the
  list of books found, for 30 days (`docs/privacy.html:33-34`).
- Sign-in is Google only; the backend stores email, Google ID, `display_name` and `avatar_url` (`backend/app/models/user.py:28-31`). There is no password.
- Frontend Sentry runs with `sendDefaultPii: false` and a scrubber
  (`frontend/lib/sentry.ts:80`, `frontend/lib/sentryScrub.ts`), and device crash events
  carry no user. Backend Sentry sets `user.id` (`backend/app/core/sentry_context.py:27-29`),
  so backend diagnostics and performance data are linked to the user. In-app feedback goes
  through `Sentry.captureFeedback` (`frontend/components/FeedbackWidget/useFeedbackSubmit.ts:79-91`).
- `ai_usage` rows record per-user AI calls for cost control; deleting an account keeps the
  cost rows with the user detached (`backend/app/models/ai_usage.py:53`).
- No third-party analytics or advertising SDK, no location, contacts, or advertising ID. First-party usage records (`ai_usage`, `scan_corrections`) are used for accuracy measurement, so Product Interaction carries the Analytics purpose.

| Data type | Collected | Linked to user | Used for tracking | Purpose | Shared with third party | Optional | Notes |
|---|---|---|---|---|---|---|---|
| Email address | Yes | Yes | No | App functionality, account management | No (Google only verifies its own token) | Required | Google-verified email |
| Name (Google `display_name`) | Yes | Yes | No | App functionality | No | Required by sign-in | Stored (`backend/app/models/user.py:30`) |
| Profile picture URL (Google `avatar_url`) | Yes | Yes | No | App functionality | No | Required by sign-in | Stored (`user.py:31`); a URL to the Google profile photo, not an image we hold: covered by Apple "Other User Content" (declared Linked in the #1001 manifest), not "Photos or Videos" |
| User ID (account ID, Google subject) | Yes | Yes | No | App functionality, fraud prevention | No | Required | Also sent to Sentry as an opaque ID |
| Photos (shelf or cover scans) | Yes | Yes (owner decision #1024: the upload is authenticated and `scan_records` keeps the result against the user for 30 days) | No | App functionality | Yes, processed by Anthropic as a service provider | Optional (feature) | Not stored beyond a 30-day fingerprint; Apple: "Photos or Videos" collected; Play: "Photos" collected and shared (service-provider exemption may apply, owner decides). |
| Other user content (library, shelves, reading status, corrections) | Yes | Yes | No | App functionality | No | Required for core use | iOS "Other User Content"; Play "Other in-app content" |
| Product interaction / usage (AI usage records) | Yes | Linked: Yes | No | App functionality, analytics (scan accuracy), cost and abuse control | No | Required | Apple "Product Interaction" (AppFunctionality + Analytics, as in the #1001 manifest) |
| Search history (book search text) | Yes | No (device breadcrumbs carry no user; access logs carry no account ID; the Google Books query is sent server-side without any user identifier) | No | App functionality | Yes: Google Books receives the search text to answer the search (`backend/app/api/books.py:71` → `services/google_books.py:63-65`, server-side, no user identifier); Sentry receives it as a service provider (device breadcrumbs) | Optional (feature) | Two separate flows. (1) **Functional:** every search sends the text to Google Books; this continues by design. (2) **Diagnostic copies:** the text also reaches device Sentry breadcrumbs and API access logs; #1029 removed these (device Sentry breadcrumbs and event URLs now drop query strings; the uvicorn access log drops them too). Removing (2) does **not** stop search text leaving our systems, because flow (1) still sends it to Google Books. **Owner decision:** with #1029 shipped, Search History may now be undeclared in the privacy manifest (`NSPrivacyCollectedDataTypeSearchHistory` in `frontend/app.json`) and the store forms, but keeping the declaration is harmless and flow (1) may still justify it. Nothing was removed. Play: "In-app search history" (shared with Google for app functionality) |
| Crash data (device crash events) | Yes | Linked: No (no user on device events) | No | App functionality | Yes, Sentry as a service provider | Required | Play: "Crash logs" |
| Performance data and other diagnostic data (backend Sentry) | Yes | Linked: Yes (`user.id` set) | No | App functionality | Yes, Sentry as a service provider | Required | Matches the #1001 manifest; Play: "Diagnostics" |
| Customer support (in-app feedback via Sentry) | Yes, only when the user submits feedback | Linked: No | No | App functionality, customer support | Yes, Sentry as a service provider | Optional | `useFeedbackSubmit.ts:79-91` |
| Device or other identifiers | No advertising ID. Device attestation tokens if enabled | No | No | Fraud prevention | Yes (Apple/Google attestation) | n/a | Revisit when attestation becomes required |
| Location, contacts, health, financial, browsing, audio | No | | | | | | Microphone permission is declared but unused; remove (#1017) so the answer stays No |

Security answers (Play): data encrypted in transit: **Yes**; users can request deletion:
**No until #291 and #1014 ship**; independent security review: **No**.

Open owner decisions before submission: whether Photos counts as "shared" with Anthropic
on Play (service-provider exemption); approve final wording; reconcile with #549 and #473.
Decided: Photos is linked to the user (#1024).

**Closed testing (verify in console):** new personal Play developer accounts have been
required to run a closed test (about 12 testers for 14 days) before production access.

## Verifying the deletion flows (#291, #1014)

Not testable yet; nothing is built. When #291 lands, re-run this audit row and check:
the in-app entry point (Settings), a confirmation step, revocation of refresh tokens,
deletion of user rows and library data, retention of detached `ai_usage` cost rows
(documented in `docs/storage-and-retention.md`), and Google-linked accounts (deleting
the app account must not require the user to still hold the Google session). The Play
web URL must work without the app installed and be reachable from the Data safety form.

## Issues filed from this audit

| Ref | Issue | Title |
|---|---|---|
| P-1 | #1014 | Public account-deletion request page for Play's web deletion URL |
| P-2 | #1015 | Update the privacy policy and link it from the app |
| P-3 | #1016 | Owner: compare the iOS privacy report from an archive with `PrivacyInfo.xcprivacy` |
| P-4 | #1017 | Remove unused permissions (`RECORD_AUDIO`, release `SYSTEM_ALERT_WINDOW`, microphone and Face ID strings) |
| P-5 | #1018 | Owner: store review package (support URL, review demo account, age and content ratings) |
| P-6 | #1019 | Owner: confirm Play's current target API requirement against targetSdk 36 |
| Review | #1023 | Ask for consent before sending scan photos to a third-party AI (Apple 5.1.2(i)) |
| Review | #1024 | Decided: Photos is linked to the user (closed) |

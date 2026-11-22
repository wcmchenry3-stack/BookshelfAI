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
(not met) · **verify** (cannot be decided from the repo; owner checks the console).
Every non-pass row links an issue.

## Summary of blockers

| Blocker | Store | Issue |
|---|---|---|
| No in-app account deletion | Apple 5.1.1(v), Play | #291 |
| No web deletion URL (Play requires one) | Play | #1014 |
| Google sign-in offered without Sign in with Apple | Apple 4.8 | #693 (secrets #679) |
| Release build signed with the debug keystore | Play | #893 |
| iOS privacy manifest declares no collected data | Apple | #1001 |
| Privacy policy omits Google sign-in, Sentry, AI usage records, deletion path | Both | #1015 |

## Apple App Store

| Item | Requirement | Status | Evidence | Issue |
|---|---|---|---|---|
| 4.8 Login services | An app that offers a third-party or social login (Google) must also offer an equivalent privacy-preserving option (Sign in with Apple, or a different option meeting the 4.8 criteria). Email and password is first-party, but the Google button triggers the rule. | **fail** | Google sign-in at `frontend/app/(auth)/login.tsx:3,37,117-127`; no Apple provider in `frontend/package.json`; backend only has `backend/app/auth/google.py` | #693 (secrets #679) |
| 5.1.1(v) Account deletion | If the app lets users create an account, it must let them start deletion inside the app. | **fail** | No account-deletion endpoint in `backend/app/` (only `DELETE /user-books/{id}` at `backend/app/api/user_books.py:400`); no UI. The privacy policy tells users to email (`docs/privacy.html:36-37`), which does not satisfy 5.1.1(v) | #291 |
| 5.1.1 Privacy policy link | Policy URL in App Store Connect metadata and reachable from inside the app. | **partial** | URL recorded as `https://wcmchenry3-stack.github.io/book_app/privacy.html` (`docs/claude/ios-appstore-submission.md:119,181`; repo has since been renamed, owner must confirm the Pages URL still resolves). No in-app link: no `privacy` string under `frontend/app`, `frontend/components`, `frontend/lib` | #1015 |
| 5.1.1 Policy accuracy | Policy must name what is collected and who receives it. | **fail** | `docs/privacy.html:11-13,26` says only account and reading data, and no analytics; it does not mention Google sign-in data, Sentry crash reports, or AI usage records. It does disclose Anthropic and the 30-day scan retention (`:33-34`) | #1015 |
| Privacy nutrition label | Label in App Store Connect matches real collection. | **verify** | Proposed answers below, pending owner approval | #473 (store privacy answers doc) |
| Privacy manifest: collected data | `NSPrivacyCollectedDataTypes` lists the data types the app (and its SDKs) collect. | **fail** | Empty: `frontend/app.json:51`, `frontend/ios/BookShelfAI/PrivacyInfo.xcprivacy:43-44`. The app collects email, name, user ID, photos, crash data | #1001 (in progress; not edited here) |
| Privacy manifest: required-reason APIs | Every required-reason API category used by the app or a bundled SDK is declared with an approved reason. | **partial** | Four categories declared (FileTimestamp, UserDefaults, DiskSpace, SystemBootTime): `frontend/app.json:33-48`, mirrored in `PrivacyInfo.xcprivacy:5-37`. React Native, Expo and Sentry SDKs ship their own manifests; the app-level list was not diffed against the built archive. **Verify**: archive, then Xcode > Product > Archive > Generate Privacy Report and compare | #1016 |
| Privacy manifest: tracking | `NSPrivacyTracking` false and no tracking domains when nothing tracks. | **pass** | `app.json:52-53`, `PrivacyInfo.xcprivacy:45-48`; no ad or analytics SDK in `frontend/package.json` | None |
| ATT (App Tracking Transparency) | Only needed if the app tracks (shares data with data brokers or ad networks, reads the IDFA). | **pass** | No ATT dependency, no `NSUserTrackingUsageDescription` in `frontend/ios/BookShelfAI/Info.plist`, no ad SDK; policy says no advertising identifiers (`docs/privacy.html:26`). Re-check whenever an analytics or ad SDK is added | None |
| Purpose strings: camera | `NSCameraUsageDescription` present and specific. | **pass** | `frontend/ios/BookShelfAI/Info.plist:55-56`, from `frontend/app.json:74-77` | None |
| Purpose strings: unused permissions | Do not declare capabilities the app does not use; reviewers query unexplained strings. | **partial** | `NSMicrophoneUsageDescription` and `NSFaceIDUsageDescription` are generic template text (`Info.plist:57-60`), and nothing in `frontend/app` uses the microphone or Face ID | #1017 |
| Purpose strings: photo library | Needed only if the app reads the photo library. | **pass** | No `expo-image-picker` or media-library dependency in `frontend/package.json`; scanning uses the camera only. If gallery upload is added, add `NSPhotoLibraryUsageDescription` and revisit the label | None |
| Encryption export compliance | `ITSAppUsesNonExemptEncryption` set so builds are not held at "Missing Compliance". | **pass** | `false` at `frontend/app.json:20` and `Info.plist:42-43`. Valid only while the app uses HTTPS and OS crypto alone; owner confirms for the annual self-classification | None |
| Support URL | Working support URL in App Store Connect metadata. | **fail** | No support page in the repo or docs. Only a contact email in the policy (`docs/privacy.html:39-40`) | #1018 |
| Demo account for review | Reviewers need to sign in. Google sign-in cannot be used by review staff without a Google account, so an email and password account with sample data is needed. | **fail** | No demo account or review notes documented anywhere under `docs/` | #1018 |
| Age rating | Questionnaire answered; policy-consistent rating. | **verify** | Not recorded. Content is user-supplied book metadata plus AI-generated text from photos, no user-to-user content. Owner completes the questionnaire; note "AI generated content" if the console asks | #1018 |
| Sign-in without friction (5.1.1(iv), 4.0) | Do not force sign-in for features that do not need it. | **verify** | Not assessed here; owner confirms the review flow reaches core value after sign-in | None |
| Data export (privacy best practice, not a store rule) | Users can get a copy of their data. | **fail** | Not built | #694 |
| Web / PWA | Web build is outside store review. | n/a | | None |

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
| Encryption | n/a on Play (Data safety "encrypted in transit" answer: yes, HTTPS only) | pass | `frontend/ios/BookShelfAI/Info.plist` ATS blocks arbitrary loads (`NSAllowsArbitraryLoads` false) | None |
| App integrity | Play Integrity usage is optional; relevant to Data safety (device identifiers). | **verify** | Attestation is wired on `/scan` but currently not required (`docs/security.md:25`). If enabled, Play Integrity tokens go to Google; mention in the answers | None |

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
- Sign-in is email plus password, or Google (email, name, Google ID verified by the backend).
- Frontend Sentry runs with `sendDefaultPii: false` and a scrubber
  (`frontend/lib/sentry.ts:80`, `frontend/lib/sentryScrub.ts`); backend Sentry tags the user
  by ID only (`docs/integrations.md:108`).
- `ai_usage` rows record per-user AI calls for cost control; deleting an account keeps the
  cost rows with the user detached (`backend/app/models/ai_usage.py:53`).
- No analytics, advertising SDK, location, contacts, or advertising ID.

| Data type | Collected | Linked to user | Used for tracking | Purpose | Shared with third party | Optional | Notes |
|---|---|---|---|---|---|---|---|
| Email address | Yes | Yes | No | App functionality, account management | No (Google only verifies its own token) | Required | Password stored hashed |
| Name (Google sign-in) | Yes | Yes | No | App functionality | No | Optional, only if Google sign-in is used | Confirm whether the backend stores it; if not stored, answer No |
| User ID (account ID, Google subject) | Yes | Yes | No | App functionality, fraud prevention | No | Required | Also sent to Sentry as an opaque ID |
| Photos (shelf or cover scans) | Yes | Yes (request is authenticated) | No | App functionality | Yes, processed by Anthropic as a service provider | Optional (feature) | Not stored beyond a 30-day fingerprint; Apple: "Photos or Videos" collected; Play: "Photos" collected and shared (service-provider exemption may apply, owner decides) |
| Other user content (library, shelves, reading status, corrections) | Yes | Yes | No | App functionality | No | Required for core use | iOS "Other User Content"; Play "Other in-app content" |
| Product interaction / usage (AI usage records) | Yes | Yes | No | App functionality, cost and abuse control | No | Required | Apple "Product Interaction" or "Other Usage Data" |
| Crash logs and diagnostics | Yes | Not linked (scrubbed, ID only) | No | Analytics (diagnostics), app functionality | Yes, Sentry as a service provider | Required | Play: "Crash logs", "Diagnostics" |
| Device or other identifiers | No advertising ID. Device attestation tokens if enabled | No | No | Fraud prevention | Yes (Apple/Google attestation) | n/a | Revisit when attestation becomes required |
| Location, contacts, health, financial, browsing, search history, audio | No | | | | | | Microphone permission is declared but unused; remove (#1017) so the answer stays No |

Security answers (Play): data encrypted in transit: **Yes**; users can request deletion:
**No until #291 and #1014 ship**; independent security review: **No**.

Open owner decisions before submission: confirm whether Google name is stored; decide the
Photos "shared" classification; approve final wording; reconcile with #549 and #473.

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

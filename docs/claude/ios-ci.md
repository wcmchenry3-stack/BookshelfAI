# iOS CI/CD — Build Infrastructure

## IMPORTANT: We use Xcode Cloud, NOT EAS Build

iOS builds and App Store submissions run through **Xcode Cloud** (Apple's CI,
`/Volumes/workspace/` on the build worker). EAS Build is NOT used — for either
iOS or Android. Do not suggest `eas build`, `eas submit`, or `eas.json` config
for any build or submission issue. `frontend/eas.json` has been deleted.

For Android build infrastructure, see [android-ci.md](android-ci.md).

## Why `pod install` must run in CI

`Pods/` is listed in `frontend/ios/.gitignore` and is never committed. Xcode
Cloud clones the repo and jumps straight to `xcodebuild archive`, which fails
with:

```
Unable to open base configuration reference file
  …/Pods/Target Support Files/Pods-Bookshelf/Pods-Bookshelf.release.xcconfig
```

## The fix: `ci_scripts/ci_post_clone.sh`

Xcode Cloud automatically executes
`frontend/ios/ci_scripts/ci_post_clone.sh` after cloning. The script:

1. Sets `SENTRY_ALLOW_FAILURE=true` so `sentry-cli` failures don't block the build
2. Runs `check_xcode_compat.sh`, which first logs the Xcode Cloud workflow, branch, tag and PR target (`CI_WORKFLOW`, `CI_BRANCH`, `CI_TAG`, `CI_PULL_REQUEST_TARGET_BRANCH`, #987), then fails in seconds if the selected Xcode is too new for the Expo SDK (see § Xcode version rollovers)
3. Installs **Node 22 LTS** from the official nodejs.org tarball, with the version pinned and the SHA-256 checked, for arm64 or x86_64 (Node 25.x ships npm 11.x, which crashes on our package-lock.json tree). It doesn't use Homebrew; see § Intel workers below
4. Persists `NODE_BINARY` to `.xcode.env.local` (this Node is not on the default PATH on Xcode Cloud — without this, the "Bundle React Native code" phase fails silently)
5. Runs `select_app_env.sh`, which points non-release builds at the dev backend (see § Which backend a build talks to)
6. Runs `npm ci` with retry logic (3 attempts, exponential back-off)
7. Runs `pod install` with retry logic (3 attempts, 30s initial delay)

File: `frontend/ios/ci_scripts/ci_post_clone.sh`

The script must be **executable** (`chmod +x`). Git preserves the execute bit
via `core.fileMode` — verify with `git ls-files --stage frontend/ios/ci_scripts/`.

**Critical**: `Podfile.lock` is committed and must NOT be deleted — using the
committed lock ensures reproducible pod versions and prevents Hermes/RN drift.

## Podfile.lock from the `Podfile lock` workflow

`Podfile.lock` must be written by CocoaPods, never by hand (CLAUDE.md rules
10-11). Linux dev containers (including Claude cloud sessions) have no
CocoaPods, so `.github/workflows/podfile-lock.yml` ("Podfile lock", job
`Generate Podfile.lock`) generates it on `macos-latest`:

- It runs on PRs that touch `frontend/ios/Podfile`, `Podfile.lock`,
  `Podfile.properties.json`, `frontend/package-lock.json`, `frontend/app.json`
  `frontend/plugins/**` or the workflow itself, and by manual dispatch (Actions → Podfile lock →
  Run workflow, pick the branch).
- It runs `npm ci`, then `pod install --repo-update` in `frontend/ios` (3
  attempts, back-off 30 s then 60 s) against the committed lock. If CocoaPods
  reports the committed snapshot as incompatible ("could not find compatible
  versions", e.g. after an RN/Expo SDK upgrade), it deletes the lock and
  resolves from scratch, as `ios-build-check` does, with a warning. It does not
  run xcodebuild; `ios-build-check` does that.
- To get the lock: open the job log and copy everything between
  `===BEGIN PODFILE.LOCK===` and `===END PODFILE.LOCK===` (both marker lines
  excluded) into `frontend/ios/Podfile.lock`, or download the `Podfile.lock`
  artifact (kept 7 days). Commit it on the PR branch.
- It fails with `::error::` while the committed lock differs from the generated
  one (`diff -u` in the log), so once the lock is committed it doubles as a
  lock-consistency check. It is not a required check.
- A "Show other files pod install changed" step lists anything else
  `pod install` rewrote (normally `project.pbxproj` build phases and
  `Info.plist`'s `RCTNewArchEnabled`). Those are informational; Xcode Cloud
  and `ios-build-check` run `pod install` themselves.

Cost: macOS minutes only on PRs that touch those paths; a run is expected to
take about 5 to 10 minutes (CocoaPods spec repo update plus `npm ci`).

`ci.yml`'s `podfile-lock-check` fails while a PR changes `Podfile` without
`Podfile.lock`, so a PR that regenerates the Podfile in a Linux session stays
red there until the generated lock is committed.

## Which backend a build talks to

An Xcode Cloud archive always builds the **Release** configuration, so the
"Bundle React Native code" phase runs Expo with `NODE_ENV=production` and loads
the committed `frontend/.env.production` (prod API, `EXPO_PUBLIC_ENVIRONMENT=production`).
`EXPO_PUBLIC_*` values are inlined into the JS bundle, so the branch alone does
not change the backend (#973).

`select_app_env.sh` (called from `ci_post_clone.sh`) decides per build:

| Build | Env used | API |
|---|---|---|
| Branch `main`, a PR into `main`, or a tag | `.env.production` | `https://bookshelfapi.buffingchi.com` |
| Anything else (e.g. the Default workflow on `dev`) | `.env.production` + generated `.env.production.local` | `https://bookshelfapi-dev.buffingchi.com`, `EXPO_PUBLIC_ENVIRONMENT=development` |

`.env.production.local` is gitignored and Expo gives it precedence over
`.env.production` key by key, so only the API URL and environment are
overridden; every other value comes from `.env.production`. The build log
shows the choice (`INFO: release build …` / `INFO: non-release build …`).
The rule is purely branch/tag based: **any** build of `main` (including a manual
"Start Build" on a `main` workflow) is treated as a release and uses prod, and
any build of another branch uses dev.
To check a TestFlight build, look at the `environment` tag on its Sentry events.

## Post-build validation: `ci_post_xcodebuild.sh`

Xcode Cloud executes `frontend/ios/ci_scripts/ci_post_xcodebuild.sh` after
`xcodebuild` completes. It validates:

1. `main.jsbundle` exists inside the `.app` bundle in the archive
2. Bundle size is > 1000 bytes (catches corrupted/empty bundles)

If either check fails, the build exits with an error. This is the final safety
net — without it, a silent bundling failure produces an app that crashes on
launch with `handleBundleLoadingError`.

## Bare workflow

`frontend/ios/` is committed (bare Expo workflow). Running `expo prebuild`
locally regenerates it; Xcode Cloud does NOT run prebuild — it uses the
committed `ios/` directory directly.

### Regenerating `ios/` with `expo prebuild`

Run `npx expo prebuild --platform ios --no-install --no-clean` from
`frontend/`. **Always pass `--no-clean`.** From SDK 58 the Expo CLI cleans by
default (`clean: !args['--no-clean']`): without it, prebuild prints "Clearing
ios", deletes `ios/` and recreates it from the template, which does not contain
our hand-kept files (this is what happened in #713). `--no-clean` applies the
changes to the existing folder instead. Never pass `--clean`. Either way, review
the whole `git diff` afterwards and restore anything prebuild removed:

| Removed or changed by prebuild | What to do |
|---|---|
| `ios/ci_scripts/*` (deleted) | `git checkout` them; check `git ls-files --stage` still shows mode `100755` |
| `ios/Podfile.lock` (deleted) | `git checkout` it; regenerate only with CocoaPods (see above) |
| `ios/BookShelfAI.xcworkspace` (deleted, `pod install` recreates it) | `git checkout` it |
| `SENTRY_ALLOW_FAILURE=true` and its comment in `ios/.xcode.env` | `git checkout` the file |
| The Sentry fallback in the "Bundle React Native code and images" build phase | Keep it (see § Sentry bundling fallback) |
| The `# @generated begin/end bookshelf-sentry-node-env` block in the "Upload Debug Symbols to Sentry" build phase | Plugin-owned (`plugins/withSentryDebugFilesNodeEnv.js`): prebuild re-inserts it, so it should be unchanged. If it is missing, the plugin is not registered before `@sentry/react-native/expo` in `app.json` (see § Known breakage — Sentry dSYM upload phase can't find Node) |
| `project.pbxproj` CocoaPods integration (Pods groups, `[Expo] Configure project`, `[CP] Embed Pods Frameworks`, `OTHER_SWIFT_FLAGS`, `REACT_NATIVE_PATH`, `USE_HERMES`, ...) | These come from `pod install`, not prebuild. Start from the committed `project.pbxproj` and apply only what prebuild adds (new source files, build-phase script changes) |
| `Info.plist` `RCTNewArchEnabled` and tab formatting | `pod install` writes them back; keep them |
| `Info.plist` `CFBundleShortVersionString` / `CFBundleVersion` (prebuild writes the literal `app.json` `version` / `ios.buildNumber`) | Restore `$(MARKETING_VERSION)` / `$(CURRENT_PROJECT_VERSION)`. `sync-native-versions.yml` updates only those build settings in `project.pbxproj`, never Info.plist, so a literal goes stale after the next release (it shipped 1.0.0 that way until #713) |
| `Images.xcassets` app icon | Prebuild regenerates it from `assets/icon.png`; the committed icon is that file (owner decision, #713), so the result should be unchanged |
| `SplashScreenLegacy`/`SplashScreenBackground` assets and `SplashScreen.storyboard` | Keep the committed ones. SDK 58 without the `expo-splash-screen` plugin points the storyboard at an image it does not create (follow-up #891) |
| The `fmt` consteval patch at the end of the Podfile `post_install` | Hand-kept (not owned by a plugin); restore it. It is a no-op when the `fmt` header does not match |

`PrivacyInfo.xcprivacy` is generated from `app.json` `ios.privacyManifests`, so
change the API reasons and the collected data types there, not in the file, then
regenerate it. A Linux prebuild writes the plist with 2-space indentation; re-indent it
with tabs like the committed file so the diff shows only the real change.
`__tests__/unit/IosPrivacyManifest.test.ts` fails if the two differ (#1001); the
declared types and their store answers are listed in
`docs/claude/ios-appstore-submission.md` (App Privacy declarations).

## JS bundle validation (GitHub Actions)

The `js-bundle-check` CI job runs `react-native bundle --platform ios` on every
PR that changes `frontend/**` (and always on push/deploy) to verify the JS bundle can be created. This catches silent bundling failures
before they reach Xcode Cloud.

`ios-build-check` runs only when native-affecting paths change (see `docs/testing.md`, "Conditional bundle and native build checks") and is skipped on draft PRs; the required check is `native-build-gate`, because a skipped reusable-workflow job never reports its `<job> / <inner job>` name. It only compiles in Debug mode, which
sets `SKIP_BUNDLING=1` — it cannot detect bundler or entry-point issues.

## Sentry bundling fallback

The "Bundle React Native code" build phase runs through `sentry-xcode.sh`, which
wraps `react-native-xcode.sh`. If `sentry-cli` fails (e.g. missing `SENTRY_ORG`)
before invoking the bundler, `SENTRY_ALLOW_FAILURE=true` masks the failure as
exit 0 — and `main.jsbundle` is never created.

A fallback in the build phase detects this and runs `react-native-xcode.sh`
directly. The `ci_post_xcodebuild.sh` script also validates the bundle exists
in the archive as a final safety net.

### Known breakage — Sentry dSYM upload phase can't find Node (build 161, #717)

Symptom (archive fails, one "Command PhaseScriptExecution failed" error in
target BookShelfAI):

```
PhaseScriptExecution Upload Debug Symbols to Sentry .../Script-B2492F90A9634265A4081F5F.sh
Script-B2492F90A9634265A4081F5F.sh: line 2: node: command not found
/bin/sh: : No such file or directory
```

Cause: `@sentry/react-native`'s Expo plugin rewrites the "Upload Debug Symbols
to Sentry" phase on every prebuild to
``/bin/sh "`${NODE_BINARY:-node} --print "...sentry-xcode-debug-files.sh"`"``.
Unlike "Bundle React Native code and images", that phase never sources
`ios/.xcode.env` / `ios/.xcode.env.local`, so `NODE_BINARY` is unset, and
Xcode Cloud has no `node` on PATH (`ci_post_clone.sh` installs Node under
`~/.bookshelf-node/` and records it only in `.xcode.env.local`). The command
substitution is empty and `/bin/sh ""` fails. Sentry 8.28 (SDK 58) added the
quotes; before that (on `dev`) the unquoted `/bin/sh` read empty stdin and
exited 0, so **dSYM upload was silently skipped**.

Fix: the config plugin `frontend/plugins/withSentryDebugFilesNodeEnv.js`
prepends a `# @generated begin/end bookshelf-sentry-node-env` block that
sources `$SRCROOT/.xcode.env` and `$SRCROOT/.xcode.env.local` (as the bundle
phase does) before Sentry's unchanged command. If Node still can't be found it
prints `warning:` and exits 0 when `SENTRY_ALLOW_FAILURE=true`, else `error:`
and exit 1. The committed `.xcode.env` always sets `SENTRY_ALLOW_FAILURE=true`,
so in practice the `exit 1` branch runs only if that line is removed. It throws
during prebuild if the Sentry phase is missing or no longer calls
`sentry-xcode-debug-files.sh`.

Side effect: because the phase now sources `.xcode.env`, Sentry's
`sentry-xcode-debug-files.sh` also sees `SENTRY_ALLOW_FAILURE=true`, so a
**real dSYM upload failure** (bad token, wrong org or project) is logged as a
warning and does not fail the archive, the same as the bundle phase. Check the
"Upload Debug Symbols to Sentry" output (or Sentry's Debug Files page) after a
release build to confirm the upload actually happened.

It must be listed **before** `@sentry/react-native/expo` in `app.json`
`plugins`: for the same mod Expo runs the last-registered plugin's action
first, so listing it earlier makes it run after Sentry's rewrite.
`__tests__/unit/withSentryDebugFilesNodeEnv.test.js` checks the plugin, that
the committed `project.pbxproj` phase equals the plugin output, the `app.json`
order, and runs the phase under `/bin/sh` with no `node` on PATH. Don't
hand-edit the phase; change the plugin and re-run prebuild (`--no-clean`).

## Xcode version rollovers

Xcode Cloud workflows set to "Latest Release" silently move to each new Xcode
major. Pin the workflow to an explicit Xcode version in App Store Connect and
bump it deliberately.

Known rollover breakage — **Xcode 27** (build 154, Sept 2026): deployment
targets below 15.0 became a hard error instead of a warning:

```
error: The iOS deployment target 'IPHONEOS_DEPLOYMENT_TARGET' is set to 11.0, but the
range of supported deployment target versions is 15.0 to 27.0.x. (in target 'Sentry-Sentry')
```

CocoaPods resource-bundle targets (`Sentry-Sentry`,
`RNCAsyncStorage-RNCAsyncStorage_resources`) inherit the podspec's minimum, not
the Podfile's `platform :ios`. `react_native_post_install` already raises pod
library targets but skips resource bundles, so the `post_install` block in
`frontend/ios/Podfile` raises only the resource-bundle targets to the app's
`platform :ios` target. That is set from `ios.deploymentTarget` in
`Podfile.properties.json` (16.4; the Podfile falls back to 16.4 if it is
unset). It never lowers a higher target. Do not remove it. When this error appears,
`ci_post_xcodebuild.sh` also logs "Could not locate .app in archive" — that is a
symptom, not the cause.

`frontend/ios/Podfile` is Expo prebuild output, so the block is owned by the
config plugin `frontend/plugins/withPodDeploymentTargetFix.js` (registered in
`app.json`), not edited by hand. `expo prebuild` (including `--clean`) re-inserts
it between the `# @generated begin/end bookshelf-pod-deployment-target` markers.
To change it, edit the plugin and re-run prebuild. The plugin throws if the Expo
template no longer has a `react_native_post_install(...)` call, and a jest test
(`withPodDeploymentTargetFix.test.js`) fails if the committed Podfile lacks the
block.

`expo prebuild` writes `Podfile.properties.json`'s `ios.deploymentTarget` from
`app.json` `ios.deploymentTarget`. Both are `16.4` since #713 (before that
`app.json` said `16.0`, so every prebuild churned the committed `16.4`). 16.4
is the floor for Expo SDK 58 (the `Expo` and `ExpoModulesCore` podspecs require
iOS 16.4; React Native 0.88 itself needs 15.1). Change the two together. The
plugin uses whatever `platform :ios` resolves to.

To verify locally after `pod install`, no target in `Pods/Pods.xcodeproj` should
have `IPHONEOS_DEPLOYMENT_TARGET` below 15.0. `ios-build-check` cannot prove
this: it runs on the runner's default Xcode (where the check is only a warning)
and deletes `Podfile.lock` first.

Known rollover breakage — **Xcode 27 vs Expo SDK 56** (build 155, Oct 2026, #708):
the archive fails in the `ExpoModulesJSI` pod's "Build ExpoModulesJSI xcframework"
phase:

```
error: the following command failed with exit code 0 but produced no further output
Command PhaseScriptExecution emitted errors but did not return a nonzero exit code to indicate failure
```

Xcode 27 reports a warning left on the nested SwiftCompile as a failure, and the
outer build fails the phase because its output contains `error:`. The xcframework
itself builds fine ("Built xcframework successfully" is logged just before). This
is upstream expo/expo#51451, fixed only in expo-modules-jsi 58.0.12. SDK 56 has no
backport, and an SDK 56 app built with Xcode 27 would also crash at launch
(`_swift_allocateMetadataPack`, expo/expo#51463; see also #598). **Do not patch
around it. Build SDK 56 with Xcode 26.x only.** Xcode 27 needs the Expo SDK 58
upgrade.

`ci_scripts/check_xcode_compat.sh` (run first by `ci_post_clone.sh`) enforces
this: if the Xcode major is 27 or higher and the `expo-modules-jsi` version in
`package-lock.json` is below 58.0.12, it fails with a pointer here. To try an
Expo upgrade on Xcode 27 on purpose, set `ALLOW_UNSUPPORTED_XCODE=1` in the
workflow's environment variables. When the SDK upgrade lands, the check passes
on its own. Raise `MIN_JSI_FOR_XCODE_27` only if upstream moves the floor.

Status (#717, 2026-11-22): `dev` is on Expo SDK 58 / React Native 0.88.1 with
`expo-modules-jsi` 58.0.23 in `package-lock.json` (#994), so
`check_xcode_compat.sh` passes on Xcode 27, and the `Default` workflow is pinned
to Xcode 27.2 (see § Current pins). The guard now only stops an old branch that
is still on SDK 56; build such a branch with Xcode 26.x, or merge `dev` into it.

GitHub's `ios-build-check` runs on the runner's default Xcode (26.x), so it stays
green while Xcode Cloud on 27 fails.

### Current pins (update whenever a workflow's Xcode Version changes)

| Workflow | Start condition | Builds | Xcode Version | Since |
|---|---|---|---|---|
| `Default` (`dev` builds; uses the dev API, see § Which backend a build talks to) | Branch changes on `dev` | Archive - iOS → TestFlight (internal) | **Xcode 27.2 (27B52)** | 2026-11-22 (#717; was 26.6 from #987) |
| `Xcode 27 – SDK 58 test` (#716) | Branch changes on `feature/717-sdk58-cutover` (last used) | Archive - iOS → TestFlight (internal) | Xcode 27.2 (27B52) | Retired at the cutover (#717): delete or disable it |

Never leave a workflow on "Latest Release" or "Latest Beta". The `Default`
workflow sat on "Latest Release" until build 159 (2026-11-22, #987): it rolled
onto Xcode 27.2 while `dev` was still on Expo SDK 56, and
`check_xcode_compat.sh` stopped the build in `ci_post_clone`.

At the SDK 58 cutover (#717, 2026-11-22), `Default` moved from 26.6 to Xcode
27.2 (27B52), the version the test workflow archived and TestFlight-tested
(build on `5418da9`, then the TestFlight build on `aac3ac9`). The test workflow
is no longer needed.

### Pinning the Xcode Cloud workflow (owner, App Store Connect)

App Store Connect → Apps → BookShelfAI → Xcode Cloud → Manage Workflows → the
workflow → **Environment** → Xcode Version → choose an explicit version (not
"Latest Release" and not "Latest Beta"). Set macOS Version to the one that goes
with it (Xcode Cloud lists the macOS versions each Xcode supports; pick the newest one it
offers) → Save. Then start a new build.

### Bumping the pin

Bump deliberately, together with the Expo SDK when the new Xcode needs it:

1. **Check compatibility.** Read the Expo SDK's supported Xcode range and the
   `check_xcode_compat.sh` floor (`MIN_JSI_FOR_XCODE_27`). An Xcode major the
   app's SDK doesn't support needs the SDK upgrade first.
2. **Try it on a test workflow first.** Duplicate the `Default` workflow, point it
   at the branch you're testing, and set the new Xcode Version. You need a green
   archive before touching `Default`.
3. **Smoke-test on TestFlight** on a real device: cold launch, Google sign-in
   and Sign in with Apple, deep links, camera scan, sign out, and a Sentry test
   error that arrives symbolicated.
4. **Move the `Default` pin** to the exact version the test workflow used, then
   delete or disable the test workflow.
5. **Update the table above** in a PR, with the date and the issue.

When you duplicate a workflow, check that it can see `SENTRY_ORG`,
`SENTRY_PROJECT` and `SENTRY_AUTH_TOKEN` (set on the workflow or as shared
environment variables). Without them, source maps aren't uploaded and the JS
bundle depends on the fallback in § Sentry bundling fallback.

## Scene-based life cycle (SDK 58)

The SDK 58 template generates `ios/BookShelfAI/SceneDelegate.swift`
(`ExpoAppSceneDelegate`) and an `Info.plist` `UIApplicationSceneManifest` that
points at it, because the iOS 27 SDK requires the scene life cycle.
`SceneDelegate` now creates the window and starts React Native; `AppDelegate`
no longer does. The template `AppDelegate` also dropped its
`application(_:open:options:)` (Linking API) and
`application(_:continue:restorationHandler:)` (Universal Links) overrides that
called `RCTLinkingManager`. Expo's `ExpoAppSceneDelegate` now forwards cold-start
URLs, `openURLContexts` and `scene(_:continue:)` to `RCTLinkingManager`, so no
manual forwarding code is needed (#598).

### URL types (`bookshelf://`)

`Info.plist` `CFBundleURLTypes` lists two schemes: `bookshelf` and the Google
reversed client id (used by `expo-auth-session`'s Google redirect). On iOS,
`app.json` `ios.infoPlist.CFBundleURLTypes` **replaces** the top-level `scheme`
(prebuild logs "Ignoring abstract property scheme"), so `bookshelf` must be
listed there explicitly as well as in the committed `Info.plist`. Guarded by
`__tests__/unit/IosUrlSchemes.test.ts`.

Device QA for deep links (Simulator or device with a TestFlight build):

1. Cold start: force-quit the app, then
   `xcrun simctl openurl booted "bookshelf://"` (add a path to test a route).
   The app must launch and land on the route.
2. Warm: leave the app backgrounded and run the same command; it must come to
   the foreground on the route without a relaunch.
3. Google sign-in still returns to the app.

## Intel workers and Homebrew (build 156, Oct 2026, #718)

Pinned to Xcode 26.x, Xcode Cloud ran the build on an **Intel x86_64** macOS
worker. Homebrew stopped shipping Intel bottles in September 2026, so
`brew install node@22` tried to build about 20 dependencies from source, and the
first one failed:

```
Warning: You are using macOS on Intel x86_64. ... Homebrew no longer builds bottles for this configuration.
configure: error: C compiler cannot create executables
```

So `ci_post_clone.sh` does not use Homebrew. It downloads the official Node
tarball for the worker's architecture (`uname -m`) and checks it against the
hash in the script. To bump Node, pick a v22.x from
<https://nodejs.org/dist/index.json> whose `npm` is 10.x, then copy both
`darwin-arm64` and `darwin-x64` hashes from that release's `SHASUMS256.txt`.
Don't add new `brew install` steps to Xcode Cloud scripts.

## Key paths on the Xcode Cloud worker

| Path | What it is |
|---|---|
| `/Volumes/workspace/repository/` | Repo root |
| `/Volumes/workspace/repository/frontend/ios/BookShelfAI.xcworkspace` | Workspace opened by Xcode Cloud |
| `/Volumes/workspace/build.xcarchive` | Output archive |
| `/Volumes/workspace/DerivedData` | Build intermediates |
| `frontend/ios/.xcode.env.local` | Generated by `ci_post_clone.sh`, persists `NODE_BINARY` (gitignored) |
| `frontend/ios/ci_scripts/ci_post_clone.sh` | Post-clone hook: Xcode compat check, backend selection, Node install (nodejs.org), npm ci, pod install |
| `~/.bookshelf-node/node-v<ver>-darwin-<arch>/` | Node used by the build (`NODE_BINARY`) |
| `frontend/.env.production.local` | Generated by `select_app_env.sh` on non-release builds: dev API override (gitignored, #973) |
| `frontend/ios/ci_scripts/select_app_env.sh` | Picks the backend per build: prod for `main`/tags, dev otherwise (#973) |
| `frontend/ios/ci_scripts/check_xcode_compat.sh` | Fails fast when the Xcode major is too new for the Expo SDK (#708) |
| `frontend/ios/ci_scripts/ci_post_xcodebuild.sh` | Post-build hook: validates main.jsbundle in archive |
| `frontend/plugins/withPodDeploymentTargetFix.js` | Config plugin: Podfile resource-bundle deployment-target fix (Xcode 27) |
| `frontend/plugins/withSentryDebugFilesNodeEnv.js` | Config plugin: loads `NODE_BINARY` before the Sentry dSYM upload phase (#717) |
| `.github/workflows/podfile-lock.yml` | GitHub Actions (not Xcode Cloud): generates `Podfile.lock` on macOS and checks the committed one |

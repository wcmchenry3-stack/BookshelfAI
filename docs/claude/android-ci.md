# Android CI/CD — Build Infrastructure

## IMPORTANT: We use local Gradle builds, NOT EAS Build

Android builds and Play Store submissions are done **locally** using
`./gradlew assembleRelease` and uploaded directly via the Play Console.
EAS Build is NOT used — for either iOS or Android. Do not suggest `eas build`,
`eas submit`, or EAS-specific config for any build or submission issue.
`frontend/eas.json` has been deleted.

## Local JDK requirement — JDK 17

**Gradle must run under JDK 17** (matches CI; JDK 21 also works). JDK 24+
breaks the Android Gradle Plugin's CMake native loading with
`A restricted method in java.lang.System has been called`, which surfaces
~7 minutes into the build at `:app:configureCMakeDebug`. `settings.gradle`
has a preflight check that fails fast if the JDK is wrong.

**Set JAVA_HOME before running any Gradle / `npx expo run:android` command:**

```bash
export JAVA_HOME=$(/usr/libexec/java_home -v 17)
```

If you don't already have JDK 17 installed:

```bash
brew install --cask zulu@17
```

Or use a version manager with the committed `.tool-versions` file at the
repo root (`java zulu-17.64.17`): `asdf install` / `mise install`.

## First-time local Android setup

After cloning, before your first `npx expo run:android`, you need two things:

**1. JDK 17 on `JAVA_HOME`** (see the section above).

**2. A local debug keystore.** `frontend/android/app/debug.keystore` is
gitignored (CI generates a throwaway one at build time). Generate one locally
with the standard Android debug credentials so AGP's default signing config
picks it up:

```bash
keytool -genkey -v \
  -keystore frontend/android/app/debug.keystore \
  -alias androiddebugkey \
  -keyalg RSA -keysize 2048 -validity 10000 \
  -storepass android -keypass android \
  -dname "CN=Android Debug,O=Android,C=US"
```

You only need to do this once per clone. If you ever see
`Keystore file '.../debug.keystore' not found for signing config 'debug'`
at `:app:validateSigningDebug`, re-run the command above.

## Why `npm ci` must run before Gradle

`node_modules/` is gitignored. Gradle's `settings.gradle` calls
`node --print require.resolve(...)` at configuration time to locate React Native
and Expo modules. If `node_modules/` is missing, Gradle configuration fails with:

```
Cannot run program "node": error=2, No such file or directory
```

Always install JS dependencies before any Gradle command — both locally and in CI.

## Bare workflow

`frontend/android/` is committed (bare Expo workflow), generated via
`expo prebuild --platform android`. When native dependencies change
(e.g. adding a new Expo module), re-run prebuild and commit the result:

```bash
cd frontend
CI=1 EXPO_OFFLINE=1 EXPO_NO_TELEMETRY=1 npx expo prebuild --platform android --no-install --no-clean
git diff -- android   # review every hunk by hand before committing
git add android/
git commit -m "chore(android): regenerate prebuild after <reason>"
```

A new native module usually changes nothing in `android/`: `settings.gradle` and
`app/build.gradle` autolink at Gradle configuration time (`expo-autolinking-settings`,
`autolinkLibrariesWithApp()`). expo-modules-autolinking finds direct dependencies and
follows their non-optional peers recursively; list a native module directly anyway
so `npx expo install` pins it to the SDK's bundled version. Check with `npx expo-modules-autolinking resolve -p android` (Expo
modules) and `npx expo-modules-autolinking react-native-config --platform android`
(other React Native libraries). Adding expo-image, expo-haptics, Reanimated,
Worklets and gesture-handler (#901) needed no `android/` change (gesture-handler,
Reanimated and Worklets were already linked as peers of `expo-router`; #901 moves
gesture-handler 3.4.0 to 3.3.0 and Worklets 0.13.1 to 0.13.0, SDK 58's versions). Run from a Linux
container, prebuild also re-encodes the `mipmap-*/ic_launcher*.webp` icons and adds
`expo_system_ui_user_interface_style` to `values/strings.xml`; neither comes from
the new dependency, so restore them (`git checkout -- android/app/src/main/res`).

Always pass `--no-clean` and never `--clean`. From SDK 58 the Expo CLI cleans
by default (`clean: !args['--no-clean']`): without `--no-clean`, prebuild
deletes `android/` and recreates it from the template, which is what happened
on the SDK 58 upgrade (#714). Template changes can still overwrite files, so
**review the diff and restore these hand edits after a prebuild**:

| File                                               | Hand edit                                                                    | Why                                                                                                                                                                                                                                                                             |
| -------------------------------------------------- | ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `android/settings.gradle`                          | JDK preflight block at the top of `pluginManagement`                         | Fails fast on JDK 24+ (book_app#178). Prebuild drops it.                                                                                                                                                                                                                        |
| `android/.gitignore`                               | `*.keystore` / `*.jks` lines                                                 | Never commit a keystore (CLAUDE.md rule 17). Prebuild drops them.                                                                                                                                                                                                               |
| `android/gradle.properties`                        | `android.enableMinifyInReleaseBuilds=true`                                   | R8 is on in release since #890 (the SDK 58 template default, so prebuild keeps it). Keep rules live in `app/proguard-rules.pro`; see below.                                                                                                                                     |
| `android/build.gradle`, `android/app/build.gradle` | Sentry Android Gradle plugin (classpath, `apply plugin`, `sentry { }` block) | Written by the `@sentry/react-native/expo` plugin from `app.json` (`experimental_android`); prebuild is idempotent here. The one hand edit is the `autoUploadProguardMapping` token guard (below).                                                                              |
| `android/gradle/wrapper/gradle-wrapper.jar`        | Official jar for the Gradle version in `gradle-wrapper.properties`           | The SDK 58 template ships the Gradle **9.3.1** jar while pinning the 9.4.1 distribution. We commit the 9.4.1 jar (sha256 `55243ef5…e1145c`, from `gradle wrapper --gradle-version 9.4.1`). It matches the gradle/actions checksum list and `@react-native/gradle-plugin`'s jar. |

The splash is not a hand edit: since #1010 the `expo-splash-screen` plugin in `app.json` (image `assets/splash-icon.png`, light and dark) makes prebuild write `drawable-*/splashscreen_logo.png`, `drawable-night-*`, `values*/colors.xml`, the `Theme.App.SplashScreen` style and the `SplashScreenManager` call in `MainActivity.kt`. Commit those; do not restore the older files over them.

`versionCode` / `versionName` in `app/build.gradle` come from `app.json`
(`expo.android.versionCode`, `expo.version`). The `sync-native-versions`
workflow patches both files with `sed`, so keep the `versionCode N` /
`versionName "x.y.z"` line format.

## Toolchain versions (Expo SDK 58 / React Native 0.88.1)

|                         | Before (SDK 56 / RN 0.85.3) | Now (SDK 58 / RN 0.88.1) | Source                                                                                                                          |
| ----------------------- | --------------------------- | ------------------------ | ------------------------------------------------------------------------------------------------------------------------------- |
| Gradle (wrapper)        | 9.0.0                       | **9.4.1**                | Expo template                                                                                                                   |
| Android Gradle Plugin   | 8.12.0                      | **9.2.1**                | `react-native/gradle/libs.versions.toml`                                                                                        |
| Kotlin Gradle plugin    | 2.1.20                      | **2.2.10** (resolved)    | RN catalog pins 2.2.0, but AGP 9 requires KGP ≥ 2.2.10 and upgrades it automatically; `android.kotlinVersion` can only raise it |
| compileSdk / buildTools | 36 / 36.0.0                 | **37 / 37.0.0**          | same catalog                                                                                                                    |
| targetSdk               | 36                          | 36                       | same catalog                                                                                                                    |
| minSdk                  | 24                          | 24                       | same catalog                                                                                                                    |
| NDK                     | 27.1.12297006               | 27.1.12297006            | same catalog                                                                                                                    |

Compatibility (CLAUDE.md rule 18): the AGP 9.2 release notes say it needs
**Gradle 9.4.1 or newer** and JDK 17 or newer, and supports API level 37.
So Gradle 9.0.0 can't run AGP 9.2.1, and 9.4.1 is the minimum.
`@react-native/gradle-plugin` (RN 0.88.1) is built with Gradle 9.4.1 too.
Don't bump the wrapper past what the AGP in RN's version catalog supports.

The template's AGP 9 opt-outs in `gradle.properties` (`android.builtInKotlin=false`,
`android.newDsl=false`) are kept. AGP 10 will remove them.

### R8 / minify in release

R8 is **on** in release builds since #890 (`android.enableMinifyInReleaseBuilds=true`,
`proguard-android-optimize.txt` preset plus `app/proguard-rules.pro`).
`android.enableShrinkResourcesInReleaseBuilds` is left unset, so resource
shrinking stays **off** (the template default): shrinking can drop resources that
React Native or Expo look up by name at runtime, and the size win is small next to
R8 and the Hermes bundle. Turn it on in a separate change with its own device pass.

**Keep rules.** Most libraries ship consumer rules inside their AAR, so
`proguard-rules.pro` only adds what is missing:

| Library                                                                                                                                                                                                                | Ships its own rules?                                           |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| `expo`, `expo-modules-core` (module registry, `Module`/`Record` classes), `expo-image` (Glide), `expo-camera`, `react-native-screens`, `react-native-svg`, `react-native-reanimated`, `react-native-worklets`          | Yes (`consumerProguardFiles`)                                  |
| React Native, Hermes, Fresco, OkHttp, `sentry-android`                                                                                                                                                                 | Yes (in their Maven AARs)                                      |
| `react-native-webview`                                                                                                                                                                                                 | **No**. We keep `@JavascriptInterface` methods and the package |
| `@sentry/react-native` Android module, gesture-handler, safe-area-context, async-storage, netinfo, expo-secure-store, expo-image-manipulator, expo-crypto, expo-file-system, expo-font, expo-haptics, expo-web-browser | No rules needed (no reflection beyond what Expo's rules keep)  |

Added in `proguard-rules.pro` (#890): the WebView JavaScript interface rules
(Turnstile runs in a WebView and the page calls the bridge by method name),
`-keepattributes LineNumberTable,SourceFile` with `-renamesourcefileattribute`
(readable frames), and `-keepattributes *Annotation*,Signature,InnerClasses,EnclosingMethod`.
Google sign-in uses `expo-auth-session` and `expo-web-browser` (JS plus a browser
tab), so there is no native Google library to keep. `@expo/app-integrity` (#1054,
not yet in `package.json`) is an Expo module covered by the `expo.modules.**` rules,
and the Play Integrity client library ships consumer rules; re-check this section
and the smoke test when #1054 lands.

**Sentry mapping upload.** The Sentry Android Gradle plugin (6.22.0, pinned by
`@sentry/react-native`) is enabled through `app.json`
(`@sentry/react-native/expo` > `experimental_android`) and written into
`android/build.gradle` and `android/app/build.gradle` by prebuild. It uploads the R8
mapping for release builds. Org, project and token come only from `SENTRY_ORG`,
`SENTRY_PROJECT` and `SENTRY_AUTH_TOKEN` (rule 21, `sentry.properties` has no
values). `autoUploadProguardMapping` is
`shouldSentryAutoUpload() && SENTRY_AUTH_TOKEN is non-empty` (hand edit in
`app/build.gradle`), so a build without the token, such as `android-build-check` or a
local build, skips the upload instead of failing. `SENTRY_DISABLE_AUTO_UPLOAD=true`
also skips it. Native-symbol and source upload are off. Debug builds have no R8
mapping, so the plugin does nothing there.

**CI coverage.** `android-build-check` runs `assembleDebug` only, so no CI job
compiles release with R8. R8 or keep-rule problems, and the Sentry plugin's release
tasks, show up only in a real `./gradlew assembleRelease` (JDK 17, with
`SENTRY_AUTH_TOKEN` set to test the upload). `android-build-check` does exercise
the Sentry plugin's configuration under AGP 9.2.1.

#### APK size (fill in from a real `assembleRelease`)

| Build                              | `app-release.apk` (MB) | arm64-v8a split / AAB download size (MB) | Notes |
| ---------------------------------- | ---------------------- | ---------------------------------------- | ----- |
| Before (R8 off, pre-#890)          | _TBD_                  | _TBD_                                    |       |
| After (R8 on, resource shrink off) | _TBD_                  | _TBD_                                    |       |

#### Release device smoke test (before shipping any R8 build)

Build with JDK 17 (`./gradlew assembleRelease`, or `bundleRelease` for Play), install
on a real device, then check:

- [ ] Cold start reaches the home screen without a crash (`adb logcat` has no `ClassNotFoundException`, `NoSuchMethodError`, `NoSuchFieldException`).
- [ ] Google sign-in completes and the session survives an app restart (SecureStore).
- [ ] Scan a book cover: camera opens, capture and image manipulation work, results show with images (expo-image).
- [ ] Wishlist: add, remove, reopen the app, list persists.
- [ ] Turnstile challenge renders in the WebView and the token reaches the app (JS interface).
- [ ] Deep links (`bookshelf://...`) open the right screen from cold start and warm.
- [ ] Forced Sentry test error (for example a temporary `Sentry.nativeCrash()` or `throw` in a debug-only trigger) shows **deobfuscated** frames with file and line numbers in Sentry. This needs the mapping uploaded: build with `SENTRY_AUTH_TOKEN`, `SENTRY_ORG`, `SENTRY_PROJECT` set, then confirm the ProGuard mapping appears under the release's debug files.
- [ ] Re-check the list above after adding any native module (including `@expo/app-integrity`, #1054).

## Signing

- **Debug keystore**: never committed. CI generates a throwaway one at build time via `keytool`. See `called-android-build-check.yml` in the shared `.github` repo.
- **Release keystore**: stored securely outside the repo. Required only for Play Store uploads, not for CI checks.
- `frontend/android/app/debug.keystore` and `upload-keystore.jks` are gitignored — keep them there.

## CI jobs (GitHub Actions)

| Job                    | What it checks                                                                                                            |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `android-bundle-check` | JS bundle compiles for Android (Metro, no native build). Runs when `frontend/**` changes                                  |
| `android-build-check`  | `./gradlew assembleDebug` compiles cleanly end-to-end. Runs only when native-affecting paths change, and not on draft PRs |
| `gradle-wrapper-check` | `gradle-wrapper.jar` is present and unmodified                                                                            |
| `local-path-check`     | No hardcoded local machine paths in Gradle files                                                                          |

The bundle/build jobs are conditional (`detect-changes`; path lists in `docs/testing.md`). The required check is `native-build-gate` (always runs; passes when each conditional job succeeded or was skipped), because a skipped reusable-workflow job never reports its `<job> / <inner job>` name. Push and deploy runs always run them.

## Play Store submission workflow

1. Increment `versionCode` in `frontend/android/app/build.gradle`
2. Run `./gradlew bundleRelease` locally to produce an AAB
3. Sign the AAB with the release keystore (stored offline)
4. Upload to Play Console → Internal Testing → promote to production

## Env vars

All runtime env vars live in `frontend/.env.production` (committed) and
`frontend/.env.local` (gitignored, for local dev). There is no `eas.json`.
A local `./gradlew assembleRelease`/`bundleRelease` loads `.env.production`, so it
talks to the **prod** API. iOS Xcode Cloud builds from `dev` override this with a
generated `.env.production.local` (see `ios-ci.md` § Which backend a build talks to);
to point a local Android release build at dev, create that file by hand.
**Never build a Play Store AAB with `frontend/.env.production.local` present** —
Gradle's bundling step would inline the dev API. Check with
`ls frontend/.env.production.local` (it must not exist) before `bundleRelease`.

## Key files

| File                                                        | Purpose                                                 |
| ----------------------------------------------------------- | ------------------------------------------------------- |
| `frontend/android/app/build.gradle`                         | App module: SDK versions, signing, dependencies, Sentry |
| `frontend/android/build.gradle`                             | Root project: repositories, plugin dependencies         |
| `frontend/android/settings.gradle`                          | Module includes, React Native + Expo autolinking        |
| `frontend/android/gradle.properties`                        | JVM args, architecture list, Hermes/New Arch toggles    |
| `frontend/android/gradle/wrapper/gradle-wrapper.properties` | Gradle distribution version                             |
| `frontend/android/sentry.properties`                        | Sentry CLI config (uses env vars for org/project/token) |
| `frontend/android/app/debug.keystore`                       | Gitignored — generated by CI, never committed           |

## Key differences from iOS CI

| Aspect           | iOS                              | Android                                   |
| ---------------- | -------------------------------- | ----------------------------------------- |
| Build system     | Xcode Cloud                      | Local Gradle (`./gradlew`)                |
| Submission       | App Store Connect                | Play Console (direct upload)              |
| Native deps      | CocoaPods (`pod install`)        | Gradle (automatic)                        |
| Lock file        | `Podfile.lock` (committed)       | None (Gradle resolves dynamically)        |
| CI compile check | `ios-build-check` (macOS)        | `android-build-check` (Linux)             |
| Bundle check     | `js-bundle-check` (iOS platform) | `android-bundle-check` (Android platform) |
| Signing          | Xcode managed                    | Keystore + env vars                       |

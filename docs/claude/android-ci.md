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
npx expo prebuild --platform android --no-install
git add android/
git commit -m "chore(android): regenerate prebuild after <reason>"
```

## Signing

- **Debug keystore**: never committed. CI generates a throwaway one at build time via `keytool`. See `called-android-build-check.yml` in the shared `.github` repo.
- **Release keystore**: stored securely outside the repo. Required only for Play Store uploads, not for CI checks.
- `frontend/android/app/debug.keystore` and `upload-keystore.jks` are gitignored — keep them there.

## CI jobs (GitHub Actions)

| Job | What it checks |
|---|---|
| `android-bundle-check` | JS bundle compiles for Android (Metro, no native build). Runs when `frontend/**` changes |
| `android-build-check` | `./gradlew assembleDebug` compiles cleanly end-to-end. Runs only when native-affecting paths change, and not on draft PRs |
| `gradle-wrapper-check` | `gradle-wrapper.jar` is present and unmodified |
| `local-path-check` | No hardcoded local machine paths in Gradle files |

The bundle/build jobs are conditional (`detect-changes`; path lists in `docs/testing.md`). The required check is `native-build-gate` (always runs; passes when each conditional job succeeded or was skipped), because a skipped reusable-workflow job never reports its `<job> / <inner job>` name. Push and deploy runs always run them.

## Play Store submission workflow

1. Increment `versionCode` in `frontend/android/app/build.gradle`
2. Run `./gradlew bundleRelease` locally to produce an AAB
3. Sign the AAB with the release keystore (stored offline)
4. Upload to Play Console → Internal Testing → promote to production

## Env vars

All runtime env vars live in `frontend/.env.production` (committed) and
`frontend/.env.local` (gitignored, for local dev). There is no `eas.json`.

## Key files

| File | Purpose |
|---|---|
| `frontend/android/app/build.gradle` | App module: SDK versions, signing, dependencies, Sentry |
| `frontend/android/build.gradle` | Root project: repositories, plugin dependencies |
| `frontend/android/settings.gradle` | Module includes, React Native + Expo autolinking |
| `frontend/android/gradle.properties` | JVM args, architecture list, Hermes/New Arch toggles |
| `frontend/android/gradle/wrapper/gradle-wrapper.properties` | Gradle distribution version |
| `frontend/android/sentry.properties` | Sentry CLI config (uses env vars for org/project/token) |
| `frontend/android/app/debug.keystore` | Gitignored — generated by CI, never committed |

## Key differences from iOS CI

| Aspect | iOS | Android |
|---|---|---|
| Build system | Xcode Cloud | Local Gradle (`./gradlew`) |
| Submission | App Store Connect | Play Console (direct upload) |
| Native deps | CocoaPods (`pod install`) | Gradle (automatic) |
| Lock file | `Podfile.lock` (committed) | None (Gradle resolves dynamically) |
| CI compile check | `ios-build-check` (macOS) | `android-build-check` (Linux) |
| Bundle check | `js-bundle-check` (iOS platform) | `android-bundle-check` (Android platform) |
| Signing | Xcode managed | Keystore + env vars |

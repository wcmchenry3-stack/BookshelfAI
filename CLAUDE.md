# Bookshelf — Claude Instructions
<!-- Global standards: ~/.claude/CLAUDE.md and ~/.claude/standards/ -->

## Stack
Expo (React Native + Web) · FastAPI · PostgreSQL · Render · GitHub Actions
Monorepo: `backend/` · `frontend/` · `.github/` · `docs/`

## Detail Files — Read Before Acting
- [Git workflow](docs/claude/git.md)      ← READ THIS FIRST
- [Backend](docs/claude/backend.md)
- [Frontend](docs/claude/frontend.md)
- [Testing](docs/claude/testing.md)
- [iOS CI/CD](docs/claude/ios-ci.md)      ← READ BEFORE touching ios/ or ci_scripts/
- [Android CI/CD](docs/claude/android-ci.md) ← READ BEFORE touching android/ or Gradle files

## Hard Rules (no exceptions)
1. NEVER push to `dev` or `main` directly.
2. ALL work goes in `feature/<name>` or `bug/<name>` — branch from `dev`.
3. After pushing: open a GitHub draft PR targeting `dev`.
4. Run tests before marking any task done.
5. No hardcoded colours — always use `useTheme()`.
6. `SecureStore` only for tokens — never `AsyncStorage`.
7. SQLAlchemy ORM only — no raw SQL.
8. Every API endpoint must have a `@limiter.limit(settings.rate_limit_*)` decorator — import `limiter` from `app.main`; use `rate_limit_auth` for auth, `rate_limit_writes` for mutations, `rate_limit_reads` for reads.
9. i18n required on all user-facing strings — use i18next + react-i18next + `i18next-resources-to-backend` (bundled locales for Expo/native); no hardcoded copy in components.

## Native Build Rules — iOS and Android
**Neither platform uses EAS Build. Do not suggest `eas build`, `eas submit`, or EAS-specific config.**
- iOS: built and submitted via **Xcode Cloud** + direct App Store Connect upload. See `docs/claude/ios-ci.md`.
- Android: built locally via `./gradlew assembleRelease` and submitted directly to **Play Console**. See `docs/claude/android-ci.md`.
- `frontend/eas.json` does not exist — it was removed. Env vars live in `.env.production` and Xcode Cloud / CI secrets.

10. Before modifying files under `frontend/ios/`: run `pod install` locally and commit `Podfile.lock` changes.
11. Never delete `Podfile.lock` — it ensures reproducible pod versions across CI builds.
12. Never change Node.js version pins (in `ci_post_clone.sh` or `package.json` engines) without verifying npm compatibility — npm 11.x has known crashes.
13. First import in `app/_layout.tsx` must always be `from '../lib/sentry'` — Sentry must self-init before any other module.
14. Read `docs/claude/ios-ci.md` before modifying `ci_post_clone.sh`, `ci_post_xcodebuild.sh`, or anything in `ci_scripts/`.
15. After any change to Podfile, `.xcode.env`, or `ci_scripts/*`, verify `js-bundle-check` and `ios-build-check` CI jobs pass before merging.
16. Before modifying files under `frontend/android/`: run `expo prebuild --platform android` locally and commit the result.
17. `frontend/android/app/debug.keystore` is gitignored — CI generates it at build time. Never commit a keystore.
18. Never change the Gradle wrapper version (`gradle-wrapper.properties`) without verifying compatibility with current AGP and RN Gradle Plugin.
19. Read `docs/claude/android-ci.md` before modifying `build.gradle`, `settings.gradle`, or `gradle.properties`.
20. After any change to `frontend/android/` or Gradle files, verify `android-bundle-check` and `android-build-check` CI jobs pass before merging.
21. `sentry.properties` must use environment variables for org/project/token — never hardcode Sentry credentials.
22. Android Gradle builds require JDK 17 locally (`export JAVA_HOME=$(/usr/libexec/java_home -v 17)`). JDK 24+ breaks CMake native loading. `frontend/android/settings.gradle` enforces this with a preflight check.

## Quick Commands
```
cd backend && uvicorn app.main:app --reload
cd backend && pytest tests/ --cov=app
cd frontend && npx expo start
cd frontend && npx jest
```

## Available Agents

These project subagents live in `.claude/agents/` and are invoked via the `Agent` tool (not Skill). Always prefer them over a general-purpose agent for their domain.

| Agent | `subagent_type` | When to use |
|---|---|---|
| lint-review | `lint-review` | Auto-fix lint issues after a lint-gate hook failure |
| plan-issues | `plan-issues` | Break a feature/bug/initiative into scoped GitHub issues — investigates code first, drafts for confirmation, then calls `gh issue create` |
| policy-compliance | `policy-compliance` | Check and fix policy violations after a policy-gate hook failure |

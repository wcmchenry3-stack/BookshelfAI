# Frontend Conventions

## Stack
Expo SDK 55 · Expo Router · React Native · TypeScript · axios · expo-secure-store

## Running Locally

```bash
cd frontend
npm install
npx expo start
# Press i (iOS), a (Android), or w (web)
```

## Key Patterns

### Theme
- **All colours must come from `useTheme()`** — zero hardcoded colour values, ever
- Import: `import { useTheme } from '../hooks/useTheme'`
- Use `theme.colors.*`, `theme.spacing.*`, `theme.typography.*`, `theme.radius.*`
- Color tokens follow the **Material Design 3** role naming convention (e.g. `primary`, `onPrimary`, `surfaceContainerLow`, `secondaryContainer`). See `frontend/theme/tokens.ts` for the full set.
- Convenience aliases `background`, `text`, `textSecondary`, `border`, `iconActive`, `iconInactive`, `success` are kept for backward compatibility but prefer MD3 role names in new code.

### Design System

#### Colors (MD3 roles — light / dark)
Defined in `frontend/theme/tokens.ts`. Key roles:

| Role | Light | Dark | Use |
|---|---|---|---|
| `primary` | `#0f426f` | `#a0cafe` | Brand, active states, filled buttons |
| `onPrimary` | `#ffffff` | `#003258` | Text/icon on primary bg |
| `secondary` | `#47645d` | `#adcdc4` | Inactive tab icons, secondary text |
| `secondaryContainer` | `#c6e7dd` | `#2f4c45` | "Read" status badge bg |
| `surface` | `#fbf9f5` | `#131210` | Screen backgrounds |
| `surfaceContainerLow` | `#f5f3ef` | `#1b1c1a` | Card backgrounds, tab bar bg |
| `surfaceContainerHighest` | `#e4e2de` | `#343532` | Input fields |
| `onSurface` | `#1b1c1a` | `#e5e3df` | Primary text |
| `onSurfaceVariant` | `#42474f` | `#c4c9c1` | Secondary text, ghost button text |
| `outline` | `#737780` | `#8e9299` | Borders, dividers |

#### Typography
- **Headline font**: Noto Serif (`NotoSerif_700Bold`, `NotoSerif_800ExtraBold`) — loaded via `useFonts` in `app/_layout.tsx` (if loading fails, e.g. the web 12s timeout, the app renders with system fonts rather than a blank screen)
- **Body / label font**: Inter (`Inter_400Regular`, `Inter_500Medium`, `Inter_600SemiBold`, `Inter_700Bold`)
- Token: `theme.typography.fontFamilyHeadline` → `'NotoSerif_700Bold'`
- Token: `theme.typography.fontFamilyBody` → `'Inter_400Regular'`
- Size scale: `fontSizeDisplay` (48) · `fontSizeH1` (40) · `fontSizeH2` (32) · `fontSizeH3` (24) · `fontSizeLG` (20) · `fontSizeBase` (16) · `fontSizeSM` (14)

#### Icons
- Library: **MaterialIcons** from `@expo/vector-icons`
- Tab icons: `document-scanner` (Scan) · `auto-stories` (Wishlist) · `library-books` (My Books) · `settings` (Settings)
- Header (`headerRight` in `InnerStack`): `dark-mode` theme toggle + feedback "?" button (`HeaderFeedbackButton`)
- Do **not** use Ionicons — it has been replaced

#### Logo / App Icon
- Source asset: `frontend/assets/logo.png` (chihuahua + open book on navy, 1024×1024)
- Configured in `app.json` as iOS icon, Android adaptive foreground (`backgroundColor: #0f426f`), and web favicon
- Rendered in-app via `<Image source={require('../assets/logo.png')} />` in the header (`app/_layout.tsx`)

### Feedback flow
- Entry points: the header "?" button (`HeaderFeedbackButton`, on every Stack screen) and the "Send feedback" row in Settings. Both call `useFeedback().openFeedback()` from `FeedbackProvider` (`components/FeedbackWidget/FeedbackContext.tsx`, mounted in `app/_layout.tsx`), which owns the single bottom-sheet `FeedbackWidget` and the `FeedbackThanksBanner`.
- Sheet: Bug / Feature request chips + description only (max 2000). On success it closes itself and the banner (4 s, non-interactive, below the header) confirms.
- Submission goes to Sentry User Feedback (`captureFeedback`, `source: 'in_app_feedback'`, tag `feedback.type`); session logs are attached for bug reports only (`SessionLogger`: 200 lines, each cut at 1,000 chars, 100 KB total, redacted before the cut: Authorization/Bearer/JWT/email/token params; never throws). Throttled per install to 5 submissions / 10 min.
- Sheet colours come from `useTheme()`; the backdrop uses the `scrim` token. Keep the sheet/body `flexShrink: 1` (not `flex: 1`) or the body collapses to zero height and the sheet renders blank.
- There is no floating button; do not add one over the tab bar.

### Accessibility
See [~/.claude/standards/accessibility.md](~/.claude/standards/accessibility.md). WCAG 2.2 AA required on every interactive element. React Native: `accessibilityLabel`, `accessibilityRole`, `accessibilityHint` (if non-obvious), `minWidth/minHeight: 44` on all `Pressable`.

### Token Storage
- Tokens and preferences: `expo-secure-store` only
- Never use `AsyncStorage` for anything sensitive

### File Routing
- Screens live in `app/` — Expo Router picks them up automatically
- Tabs: `app/(tabs)/<name>.tsx`
- Auth: `app/(auth)/<name>.tsx`
- Modals: `app/<name>.tsx` with `presentation: 'modal'`

### Internationalisation (i18n)

Stack: `i18next` + `react-i18next`. All user-facing strings must use `useTranslation()`.

#### Namespaces

| Namespace | Covers |
|-----------|--------|
| `common` | Error title, Close, Remove, cover alt |
| `auth` | Login screen |
| `tabs` | Tab bar labels + a11y labels |
| `my-books` | My Books screen — status tabs, badge, detail sheet, actions |
| `scan` | Scan screen — alerts, mode tabs, permission, camera |
| `settings` | Settings screen |
| `wishlist` | Wishlist screen |
| `components` | `BookCandidatePicker`, `ThemeToggleButton` |
| `feedback` | In-app feedback widget |

#### Using translations in a screen

```tsx
import { useTranslation } from 'react-i18next';

const { t } = useTranslation('my-books');
// Cross-namespace lookup:
t('close', { ns: 'common' })
// Interpolation:
t('coverAlt', { ns: 'common', title: book.title })
// Dynamic nested key:
t(`status.${item.status}`)
```

#### Adding a new language

1. Add locale object to `LOCALES` in `src/i18n/locales.ts`
2. Run translate script for each namespace:
   ```bash
   cd frontend
   OPENAI_API_KEY=... node scripts/translate.js --locale <code> --namespace <ns>
   # or all namespaces at once for one locale:
   OPENAI_API_KEY=... node scripts/translate.js --locale <code> --namespace common && ...
   ```
3. Add 9 static imports (one per namespace) to `src/i18n/i18n.ts` and add locale entry to `resources`
4. If RTL (ar/he), add to `RTL_LOCALES` in `locales.ts`

#### Re-running translations (e.g. after adding keys)

```bash
# Translate missing keys only:
node scripts/translate.js --locale es --namespace scan
# Retranslate all keys (force):
node scripts/translate.js --locale es --namespace scan --force
# Preview without writing:
node scripts/translate.js --locale es --namespace scan --dry-run
# All locales × all namespaces:
node scripts/translate.js --all
```

#### Key parity (every `en` key in every locale)

`__tests__/unit/localeKeyParity.test.ts` fails with a list of `ns:key` entries when a locale is missing a key that `en` has, or carries a stale key that `en` no longer has. When you add or remove an `en` key, update all 12 other locales in the same PR (and `_meta/<ns>.meta.json`). Plurals: `en` uses `x_one` / `x_other`; a locale satisfies `x` with `x_other` plus its own CLDR categories (ar: zero/one/two/few/many/other, ru: one/few/many/other, he: one/two/many/other, ja/ko/zh: other only). `localeCharacterLimits.test.ts` enforces the `characterLimit` values in `_meta`. `provenance.json` currently tracks only the `feedback` namespace.

#### RTL locales
Arabic (`ar`) and Hebrew (`he`) are RTL. String translations are complete.
Layout mirroring (`I18nManager.forceRTL()` + app restart) is a follow-up task.

### Scan quota states

`lib/scanErrors.ts` is the only place that knows the `POST /scan` refusal codes. `classifyScanError(err)` maps an axios error to exactly one UI state (`daily_limit`, `requests_daily_limit`, `in_progress`, `paused`, `unavailable`, `enhanced_refused`, `generic`; the table is in the file header); it distinguishes by `response.data.detail`, not status alone (429 is `daily_scan_limit`, `scan_in_progress` and `enhanced_throttled`). There is no monthly limit (#582): no `monthly_limit` state, notice or copy; a 402 from an older server (`scan_limit_reached` / `enhanced_limit_reached`) is `generic`. `enhanced_refused` (#533: `enhanced_requires_premium`, `enhanced_throttled`) has no notice card: `ScanJobContext` shows a banner (`enhancedRefusalMessage`), falls back to the standard results and stops only enhanced photos in the drain. The enhanced re-scan is offered only when `enhancedScansRemaining` (from `enhanced_scans_remaining` on scan responses: the Premium daily allowance left since #582) is above 0. `toScanNotice` / `scanNoticeMessage` turn a state into the notice the scan screen shows. `ScanJobContext` owns the `scanNotice` and `usageRefreshKey`; the upload itself is `executeScan(deps, job)` in `lib/scan/executeScan.ts` (the provider rebuilds its typed `ScanExecDeps`, `lib/scan/types.ts`, in an effect and assigns `executeScanRef`; refs are read at call time), with the refusal / retry-schedule / enhanced-refusal handlers and error-classification helpers in `lib/scan/scanOutcomes.ts`. The silent `scan_in_progress` retry (in `executeScan`) (max 3, honouring `Retry-After`); `hooks/useScanUsage.ts` fetches `GET /scan/usage` for `components/ScanStatusPanel.tsx` (scan screen) and the Settings row, and returns `null` offline or on failure so the counter just hides. Two counters (#577): `remainingToday` (found, falling back to `used_today` / `daily_limit` from an older server) and `noMatchRemainingToday` (`null`, so the line hides, when the server sends no `no_match_*`). The no-match banner copy comes from `noMatchMessage` (`lib/scanJob.ts`) on the scan response's `no_match`. `unavailable` (503 `scan_unavailable`, #604; also the Turnstile refusals `400 "Missing Turnstile token"` / `403 "Turnstile verification failed"`, codes `turnstile_missing` / `turnstile_failed` in `TURNSTILE_REFUSALS`, #740) is the only retryable state: `ScanJobContext` retries the photo on `SCAN_RETRY_DELAYS_MS` (in `scanErrors.ts`, the one place for the delays) and stores `attempts` / `nextAttemptAt` on the job. Add new backend codes in `scanErrors.ts` and its table-driven test, never inline in a component.

### Turnstile on POST /scan (#740)

`contexts/TurnstileContext.tsx` (`TurnstileProvider`, mounted once in `app/_layout.tsx` above `ScanJobProvider`) gives `useTurnstile().getToken(signal)` (a fresh token or `null`, never a rejection) and `configured` (false without a site key or native base URL; `ScanJobContext` then treats `400 "Missing Turnstile token"` as final, not retryable). `ScanJobContext` calls it before every `POST /scan` (retries included) and appends `cf-turnstile-response` only when it got one; tokens are single-use, so never reuse or cache one. The logic lives in `lib/turnstile/` (`config.ts` env + constants, `tokenBroker.ts` one challenge at a time with a 10 s timeout and abort, `navigationGuard.ts`, `widgetHtml.ts` the native inline page, `webScript.ts` / `webWidget.ts` web). The widget is `components/TurnstileWidget.tsx` (native: one hidden, locked-down `react-native-webview`; `originWhitelist={['*']}` with all filtering in `onShouldStartLoadWithRequest`; a broken page is remounted by a `key` bump with backoff `TURNSTILE_REMOUNT_DELAYS_MS`, never `reload()`) and `components/TurnstileWidget.web.tsx`. Env: `EXPO_PUBLIC_TURNSTILE_SITE_KEY` (unset: no widget, no token, one breadcrumb), `EXPO_PUBLIC_TURNSTILE_BASE_URL` (native page origin; default the origin of `EXPO_PUBLIC_API_URL`, https only, or http for localhost). No UI, no copy. Tests: `jest.setup.ts` mocks `react-native-webview` (a View with the WebView's props, testID `turnstile-webview`, hidden: query with `includeHiddenElements: true`; ref calls on `__mock.injectJavaScript` / `__mock.reload`, each new instance on `__mock.mount`); without a `TurnstileProvider` `getToken` returns `null`, so other scan tests need nothing. See `__tests__/unit/ScanTurnstile.test.tsx` and `turnstile.test.tsx`; the web path is under jsdom (`turnstileWeb.test.ts`, `ScanTurnstileWeb.test.tsx`).

### Sentry noise filter (#701)

`lib/sentry.ts` passes `beforeSend` = `filterExpectedNetworkNoise` from `lib/sentryFilter.ts`. It drops (returns `null`, after leaving a `sentry.filter` info breadcrumb) only two web signatures (mechanism `auto.browser.global_handlers.onunhandledrejection`; the native React Native `onunhandledrejection` is kept) that come from the user's network or an ad-blocker, not from a bug we can fix:

- the font loader's fixed `"12000ms timeout exceeded"` (expo-font / fontfaceobserver). `beforeSend` runs before symbolication and all first-party code is minified into the same `app:///entry-<hex>.js` bundle (as in the real BC_BOOKSHELF-J event), so this is effectively matched by message + mechanism only; the frame check merely rejects events that carry a source-file frame (any frame that is not a bundle-entry or anonymous filename);
- a bare `NetworkError` DOMException (`A network error occurred.`, code 19) with no stack frames. This assumes our own network calls never surface as a stackless bare `NetworkError` DOMException (axios raises `AxiosError` `Network Error`; fetch raises `TypeError`).

Kept: our API client's axios errors (`Network Error`, `timeout of 30000ms exceeded`), any event with a source-file frame, any event with more than one exception (multi-exception events are always kept), other timeouts and network errors, and any malformed event (the filter never throws; unknown shape means keep). The matcher is `isExpectedClientNetworkNoise(event, hint)`. To widen it, add a signature there plus a "dropped" and a "kept" case in `__tests__/unit/sentryFilter.test.ts`; keep it narrow, and if these errors show up with real first-party frames treat that as a bug (reopen #660), not as something to filter. BC_BOOKSHELF-J's shape (entry-hash frames, type `Error`, `12000ms timeout exceeded`) is from a real captured event; BC_BOOKSHELF-H's is as reported in the issue (type `Error`, `NetworkError: A network error occurred.`, code 19, no stacktrace), so check a real event in Sentry before widening.

### Sentry event scrubbing (#736)

`sentryBeforeSend` (`lib/sentry.ts`) runs the noise filter (a dropped event returns `null` and is never scrubbed), then `scrubSentryEvent` (`lib/sentryScrub.ts`). A raw AxiosError no code catches still reaches Sentry's unhandled-rejection handler and carries `config.headers.Authorization` plus request/response bodies. The SDK does not attach them today (`sendDefaultPii: false`, no ExtraErrorData), but an added integration or SDK upgrade could. Rules:

- Import graph: `sentry.ts` imports only `@sentry/react-native`, `sentryFilter` and `sentryScrub`; `sentryScrub` imports only the leaf `apiErrorSanitize` (which owns `sanitizeApiError` / `pathTemplate`; `apiErrorReporting` re-exports them). No axios import (AxiosError is detected by `isAxiosError === true`), so Sentry still self-inits before any other module (rule 13). Keep `apiErrorSanitize` free of imports that reach `./sentry`.
- When `hint.originalException` is an AxiosError, only the last `exception.values` entry (the error itself, not its `cause` chain) becomes the sanitized `ApiError`.
- `extra`, `contexts`, `logentry`, `message`, top-level `event.request` and each `mechanism.data` are rebuilt, never mutated (frozen events work); the original object is returned when nothing needed scrubbing. Always redacted at any depth, case-insensitive: `authorization`, `cookie`, `set-cookie`, `proxy-authorization`, `token`, `jwt`, `access_token`/`accessToken`, `refresh_token`/`refreshToken`, `id_token`/`idToken`, `password`, `passwd`, `secret`, `api_key`, `apikey`, `api-key`. `headers` and `data` under any `response` key are redacted unconditionally; under `config`/`request` only inside an object that has `url`/`method`/`status`. `config` as a whole when it has `headers`, `url`, `data`, `baseURL` or `method`; `request` only when it looks like an XHR/http request (has `headers`, `_headers`, `_header`, `responseURL`, `readyState`, `body` or `data`), so `contexts.request: {method}` survives. On `event.request`: header keys by the same rules, `url`/`query_string` by string redaction, `cookies` redacted.
- Breadcrumbs: `beforeBreadcrumb` (`sentryBeforeBreadcrumb`) runs `scrubBreadcrumb` on `message` and `data` (small objects, off the frame path). The #701 `sentry.filter` breadcrumb has no sensitive content and passes through unchanged.
- Only plain objects and arrays are recursed (depth 8; ancestor-path cycle check, so shared references are fine and only true cycles become `[Circular]`). `Error` becomes `{name, message}`, `Date` is kept, Map/Set/class instances become `[Object]`.
- Strings (`redactString`): `Authorization:`/`Proxy-Authorization:` header lines -> `Authorization: [Filtered]`, `Cookie:`/`Set-Cookie:` lines -> `Cookie: [Filtered]`, `Bearer <token>` -> `Bearer [Filtered]`, raw JWTs -> `[jwt]`, `access_token|refresh_token|id_token|token|api_key|password|secret=` query values -> `[Filtered]`. All linear-time: JWTs are found by a manual scan (`indexOf('eyJ')`, skip a candidate preceded by a token char, three dot-separated segments, and a failed candidate jumps past its whole token run); the other patterns are regexes using lookahead + backreference (possessive emulation) whose matches consume what they match. `sentryScrub.test.ts` checks the ratio of the time at ~40k vs ~10k chars stays under 8 for `eyJ`, `eyJa.`, `eyJa.eyJb.`, `Bearer`, `token=`, `Authorization:` and `Cookie:` repeats.
- Fail closed: each field is scrubbed in its own try block; one that throws is deleted from the event, never sent half-scrubbed. If the event cannot even be copied, `scrubSentryEvent` returns `null` and the event is dropped (no safe copy can be built, and sending it unscrubbed is worse).
- Not covered: on web, `event.request`'s page URL gets only query-param redaction and the `Referer` header only string redaction; a token in a URL path or fragment is not found. No tokens travel in page URLs today; revisit if that changes.

Tests (`__tests__/unit/sentryScrub.test.ts`) use a real `BrowserClient` with a capturing transport and assert the JWT is absent from the serialized envelope; the control case shows it is present without the scrubber.

### API error reporting (#724)

`lib/api.ts`'s response interceptor calls `reportApiError` (`lib/apiErrorReporting.ts`) for every non-401 failure. It captures a sanitized `ApiError` (never the AxiosError, which carries the `Authorization` header and bodies) with tags `http.status`, `http.method`, `api.path` (template: no origin, query, UUIDs or numeric IDs -> `:id`) and `api.detail` (only for allowlisted machine codes; in practice `forbidden`, since 401 is never reported), fingerprinted by those. Once per (method, path template, status, detail) per app session (module-level Set), so Sentry volume is bounded by distinct endpoints x codes. Reported: 403 and 5xx. Not reported: network errors (no response, #701), 401 (refresh flow), 402/409/429 and other handled 4xx, handled business codes (`enhanced_requires_premium`, scan refusals). `POST /scan` is reported here like any other path: this reporter is the single owner of every 403/5xx, and ScanJobContext skips its own capture when `isReportedCentrally(err)` (it still captures no-response errors and other statuses such as 422, as a sanitized `ApiError` via `sanitizeApiError`). A `403 forbidden` (edge/origin lockdown) is not an auth failure: it never signs out, and `isServiceUnavailable(err)` selects the translated `common:serviceUnavailable` message on My Books, Wishlist and the scan failure banner. Add new allowlisted codes in `apiErrorReporting.ts` plus a case in `__tests__/unit/ApiErrorReporting.test.ts`.

### Calling the API: timeouts, retry, `ApiError` (#766)

Call the backend through the `api` instance in `lib/api.ts` (never bare `axios`, which skips the token, refresh and retry interceptors).

- **Timeouts** (`lib/apiTimeouts.ts`, derived from the server's own bounds): `DEFAULT_TIMEOUT_MS` = 30 s on the instance (database-only endpoints); `GET /books/search` passes `SEARCH_TIMEOUT_MS` = 45 s (Google Books 10 s + enrichment deadline 20 s); `POST /scan` passes `SCAN_UPLOAD_TIMEOUT_MS` = 180 s (AI timeout up to 120 s + enrichment 20 s + upload). Any new endpoint that calls a provider needs its own per-call timeout above the server's worst case.
- **Retry** (response interceptor in `lib/api.ts`): up to `MAX_RETRIES` = 2 retries (3 attempts in all) of network errors (no response; offline, refused, reset), 408, 429, 502, 503 and 504, after `1 s * 2^n` plus up to 25% jitter (about 1 s, then 2 s). A `Retry-After` header (seconds or HTTP-date) replaces the backoff, capped at `MAX_RETRY_AFTER_MS` = 30 s. Only GET, HEAD, PUT, DELETE and OPTIONS retry. POST and PATCH never do (TODO #685: once the server honours an `Idempotency-Key`). `/scan` never retries here, whatever the method: the scan queue (#604) owns its retry schedule. A timeout (`ECONNABORTED`) is never retried: the server may still be working, and a re-send multiplies load. Canceled requests, and requests from an earlier session (sign-out meanwhile), are not retried. A 401 is not a retry: it goes through the single-flight refresh. Tests swap the sleeper and jitter with `configureRetry({ sleep, random })` (call `configureRetry()` to reset).
- **Sentry**: the interceptor reports only the final failure of a logical request (see "API error reporting" above); retried attempts are never reported.
- **Handling errors**: convert with `toApiError(err)` from `lib/apiError.ts` instead of reading `err.response` by hand. It returns `{ status?, code?, retryAfterMs?, isNetworkError, isTimeout, message }`. `code` is the backend `detail` when it is a string, or `detail.code` / `detail.detail.code` when it is an object; `retryAfterMs` is the parsed, uncapped `Retry-After`. `message` is for logs only; show translated copy keyed on `status`/`code`. Existing callers still read raw axios errors (`lib/scanErrors.ts`, `lib/trash.ts`, the auth and wishlist screens); migrate them as you touch them.

## Commands

```bash
npx jest                          # unit tests
npx jest --coverage               # with coverage report
npx eslint .                      # lint (includes a11y rules)
npx prettier --check .            # format check
npx prettier --write .            # format fix
```

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
- **Headline font**: Noto Serif (`NotoSerif_700Bold`, `NotoSerif_800ExtraBold`) — loaded via `useFonts` in `app/_layout.tsx`
- **Body / label font**: Inter (`Inter_400Regular`, `Inter_500Medium`, `Inter_600SemiBold`, `Inter_700Bold`)
- Token: `theme.typography.fontFamilyHeadline` → `'NotoSerif_700Bold'`
- Token: `theme.typography.fontFamilyBody` → `'Inter_400Regular'`
- Size scale: `fontSizeDisplay` (48) · `fontSizeH1` (40) · `fontSizeH2` (32) · `fontSizeH3` (24) · `fontSizeLG` (20) · `fontSizeBase` (16) · `fontSizeSM` (14)

#### Icons
- Library: **MaterialIcons** from `@expo/vector-icons`
- Tab icons: `document-scanner` (Scan) · `auto-stories` (Wishlist) · `library-books` (My Books) · `settings` (Settings)
- Header: `dark-mode` for theme toggle
- Do **not** use Ionicons — it has been replaced

#### Logo / App Icon
- Source asset: `frontend/assets/logo.png` (chihuahua + open book on navy, 1024×1024)
- Configured in `app.json` as iOS icon, Android adaptive foreground (`backgroundColor: #0f426f`), and web favicon
- Rendered in-app via `<Image source={require('../assets/logo.png')} />` in the header (`app/_layout.tsx`)

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
3. Add 8 static imports to `src/i18n/i18n.ts` and add locale entry to `resources`
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

#### RTL locales
Arabic (`ar`) and Hebrew (`he`) are RTL. String translations are complete.
Layout mirroring (`I18nManager.forceRTL()` + app restart) is a follow-up task.

### Scan quota states

`lib/scanErrors.ts` is the only place that knows the `POST /scan` refusal codes. `classifyScanError(err)` maps an axios error to exactly one UI state (`daily_limit`, `requests_daily_limit`, `in_progress`, `paused`, `unavailable`, `enhanced_refused`, `generic`; the table is in the file header); it distinguishes by `response.data.detail`, not status alone (429 is `daily_scan_limit`, `scan_in_progress` and `enhanced_throttled`). There is no monthly limit (#582): no `monthly_limit` state, notice or copy; a 402 from an older server (`scan_limit_reached` / `enhanced_limit_reached`) is `generic`. `enhanced_refused` (#533: `enhanced_requires_premium`, `enhanced_throttled`) has no notice card: `ScanJobContext` shows a banner (`enhancedRefusalMessage`), falls back to the standard results and stops only enhanced photos in the drain. The enhanced re-scan is offered only when `enhancedScansRemaining` (from `enhanced_scans_remaining` on scan responses: the Premium daily allowance left since #582) is above 0. `toScanNotice` / `scanNoticeMessage` turn a state into the notice the scan screen shows. `ScanJobContext` owns the `scanNotice`, the silent `scan_in_progress` retry (max 3, honouring `Retry-After`) and `usageRefreshKey`; `hooks/useScanUsage.ts` fetches `GET /scan/usage` for `components/ScanStatusPanel.tsx` (scan screen) and the Settings row, and returns `null` offline or on failure so the counter just hides. Two counters (#577): `remainingToday` (found, falling back to `used_today` / `daily_limit` from an older server) and `noMatchRemainingToday` (`null`, so the line hides, when the server sends no `no_match_*`). The no-match banner copy comes from `noMatchMessage` (`lib/scanJob.ts`) on the scan response's `no_match`. `unavailable` (503 `scan_unavailable`, #604) is the only retryable state: `ScanJobContext` retries the photo on `SCAN_RETRY_DELAYS_MS` (in `scanErrors.ts`, the one place for the delays) and stores `attempts` / `nextAttemptAt` on the job. Add new backend codes in `scanErrors.ts` and its table-driven test, never inline in a component.

## Commands

```bash
npx jest                          # unit tests
npx jest --coverage               # with coverage report
npx eslint .                      # lint (includes a11y rules)
npx prettier --check .            # format check
npx prettier --write .            # format fix
```

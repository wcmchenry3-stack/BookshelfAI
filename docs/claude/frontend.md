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
- New code uses the **v2 colour roles** (`background`, `surface`, `surfaceBar`, `field`, `onSurface`, `onSurfaceVariant`, `outline*`, `primary`, `onPrimary`, `primarySoft`, `danger*`, `success`, the status dots, `coverBlank` / `onCoverBlank`; table below). They keep the MD3 role naming style, but the older MD3 names (`surfaceContainerLow`, `secondaryContainer`, ...) are temporary aliases. See `frontend/theme/tokens.ts` for the full set.
- **Design tokens v2 (#898)**: the approved values live in `palette` (`theme/tokens.ts`); `tokens.colors` exposes each role as `{ light, dark }` and `light.ts` / `dark.ts` resolve them with `colorsFor(mode)`. New code uses the v2 roles (table below).
- **Temporary aliases (sunset plan):** the older MD3 roles (`primaryContainer`, `secondary*`, `tertiary*`, `surfaceContainer*`, `error*`, `outlineVariant`, ...) and the convenience aliases `background`, `text`, `textSecondary`, `border`, `iconActive`, `iconInactive` are mapped onto the v2 palette so untouched screens compile and pick up the new colours. Do not use them in new code; a follow-up removes each alias once no caller is left (`grep -rn "colors\.<alias>" app components`).
- **Single accent:** `primary` is the only accent hue, in both modes. There is no gold dark-mode accent any more; `tertiary*` map onto `primary` / `primarySoft` (UnverifiedBadge's `tertiaryFixed` chip is now `primarySoft` with `primary` text). Never pick a colour by `theme.isDark`.
- **Repo-wide colour check:** `__tests__/unit/noColourLiterals.test.ts` scans `app/`, `components/`, `contexts/`, `hooks/` and `lib/` (not `theme/` or tests) and fails on any string literal containing a hex colour (`#` + 3/4/6/8 hex digits) or an `rgb()/rgba()/hsl()/hsla()` call, anywhere inside the string (e.g. `'1px solid #fff'`), printing `file:line`. Comments are ignored; named colours such as `'white'` are not detected. There is no allowlist or baseline. It complements the org-level shared `design-token-check.yml` workflow.
- **Translucent / fixed tokens:** `headerOverlay` (no longer used by the root header since #906, which made it opaque; kept for now: dark `background` at 70%, light transparent), `tabBarOverlay` (`surfaceBar`; 85% in dark), `outlineVariantFaint` (dark `outline` at 40%; light transparent), `shadow`, and the `errorBoundary*` set (v2 roles per mode: `background`, `onSurface`, `onSurfaceVariant`, `danger`, `dangerSoft`, `primary`, `onPrimary`). In practice the ErrorBoundary always shows the light palette: it mounts above `ThemeProvider`, so it gets `ThemeContext`'s default (`lightTheme`). Define a new token in `tokens.ts` with both modes; `light.ts` and `dark.ts` pick it up automatically.
- **ErrorBoundary** (a class, mounted above `ThemeProvider`) reads the theme via `ThemeContext.Consumer` and falls back to `lightTheme` if the context value is missing or malformed, so the error screen can never crash on theming.

### Design System

#### Colors (v2 roles — light / dark, #898)

Defined in `frontend/theme/tokens.ts` (`palette`).

| Role                                                                  | Light                                         | Dark                                          | Use                                                                                                                                                                                                                                 |
| --------------------------------------------------------------------- | --------------------------------------------- | --------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `background`                                                          | `#F6F4EF`                                     | `#121417`                                     | Screen background                                                                                                                                                                                                                   |
| `surface`                                                             | `#FFFFFF`                                     | `#1B1E23`                                     | Cards, sheets                                                                                                                                                                                                                       |
| `surfaceBar`                                                          | `#FBFAF7`                                     | `#16181C`                                     | Tab bar, top bars                                                                                                                                                                                                                   |
| `field`                                                               | `#ECE8DF`                                     | `#1F2227`                                     | Inputs, search, secondary buttons                                                                                                                                                                                                   |
| `onSurface`                                                           | `#1A1C20`                                     | `#ECE9E3`                                     | Primary text                                                                                                                                                                                                                        |
| `onSurfaceVariant`                                                    | `#5E636B`                                     | `#A4A8AF`                                     | Secondary text, inactive icons, placeholders                                                                                                                                                                                        |
| `outline`                                                             | `#E4DFD4`                                     | `#2A2D33`                                     | Hairlines, dividers (not for text)                                                                                                                                                                                                  |
| `outlineCard`                                                         | `#ECE7DC`                                     | `#2A2D33`*                                    | Card borders                                                                                                                                                                                                                        |
| `outlineChip`                                                         | `#DAD4C7`                                     | `#33363C`                                     | Chip borders                                                                                                                                                                                                                        |
| `outlineField`                                                        | `#857F73`                                     | `#7D786F`                                     | Input borders (TextInput, search fields, outlined buttons). Meets WCAG 1.4.11 3:1 against `field`, `surface` and `background` (light 3.25 / 3.98 / 3.62, dark 3.64 / 3.81 / 4.21). Use it, not `outline`, for any control boundary. |
| `primary`                                                             | `#0F426F`                                     | `#A9C9EE`                                     | The single accent: active states, filled buttons, links                                                                                                                                                                             |
| `onPrimary`                                                           | `#FFFFFF`                                     | `#0B2540`                                     | Text/icon on `primary`                                                                                                                                                                                                              |
| `primarySoft`                                                         | `#DCE6F1`                                     | `#22344A`                                     | Active pill, soft accent chips                                                                                                                                                                                                      |
| `danger` / `onDanger`                                                 | `#B3261E` / `#FFFFFF`                         | `#FFB4AB` / `#690005`*                        | Destructive actions, errors                                                                                                                                                                                                         |
| `dangerSoft`                                                          | `#F7E3DF`                                     | `#3A2220`*                                    | Error banners                                                                                                                                                                                                                       |
| `success` / `onSuccess`                                               | `#2F7A4F` / `#FFFFFF`                         | `#7FC59B` / `#10291B`*                        | Success states                                                                                                                                                                                                                      |
| `statusWishlist` · `statusPurchased` · `statusReading` · `statusRead` | `#A56A12` · `#5F6F86` · `#0F426F` · `#2F7A4F` | `#E0B062` · `#A3B1C6` · `#A9C9EE` · `#7FC59B` | Status dots                                                                                                                                                                                                                         |
| `coverBlank` / `onCoverBlank`                                         | `#E7E1D3` / `#4A4740`                         | `#2A2C30` / `#C9C4BA`                         | Cover placeholder                                                                                                                                                                                                                   |

\* Dark values not in the approved list, chosen to pass AA; pending owner review.

`__tests__/unit/themeTokensV2.test.ts` checks every value above, that every token exists in both themes, and WCAG AA (4.5:1) for `onSurface` / `onSurfaceVariant` / `primary` on `background` and `surface`, `onPrimary` on `primary`, `onCoverBlank` on `coverBlank` (and a few more pairs), in both themes.

#### Spacing and radii

- `theme.spacing`: `xs` 4 · `sm` 8 · `ms` 12 · `md` 16 · `lg` 24 · `xl` 32 · `xxl` 48.
- `theme.radius`: `cover` 3 · `coverLarge` 5 · `field` 10 · `button` 12 · `card` 14 · `sheet` 16 · `chip` / `pill` full (9999). The older `sm`/`md`/`lg`/`xl`/`full` scale stays while screens move over.

#### Typography (#899, #912)

- **Font-file rule:** custom fonts ship one file per weight and Android ignores `fontWeight` on a custom family, so a weight is chosen by naming its font FILE. Never style text with `fontWeight`. Use `AppText` with a `variant`, or `fontFor(weight, family)` from `theme/typography.ts` in a StyleSheet (`fontFor('600')` -> `Inter_600SemiBold`, `fontFor('700', 'serif')` -> `NotoSerif_700Bold`; serif has 600 and 700 only).
- Loaded in `app/_layout.tsx` `useFonts`: `NotoSerif_600SemiBold`, `NotoSerif_700Bold`, `Inter_400Regular`, `Inter_500Medium`, `Inter_600SemiBold`, `Inter_700Bold` (no unused files; 800 was dropped). If loading fails (e.g. the web 12 s timeout) the app renders with system fonts rather than a blank screen.
- Variants (`textVariants`, also `theme.typography.variants`):

| Variant                       | Font file                               | Size / line | Notes                            |
| ----------------------------- | --------------------------------------- | ----------- | -------------------------------- |
| `screenTitle`                 | `NotoSerif_700Bold`                     | 32 / 40     | tracking -0.4                    |
| `sheetTitle`                  | `NotoSerif_700Bold`                     | 24 / 30     |                                  |
| `bookTitle`                   | `NotoSerif_600SemiBold`                 | 16 / 21     |                                  |
| `body`                        | `Inter_400Regular`                      | 16 / 21     | default                          |
| `bodyStrong`                  | `Inter_600SemiBold`                     | 16 / 21     |                                  |
| `secondary`                   | `Inter_400Regular`                      | 14 / 18     |                                  |
| `meta`                        | `Inter_400Regular`                      | 13 / 16     |                                  |
| `sectionLabel`                | `Inter_600SemiBold`                     | 13 / 16     | uppercase, tracking +0.6, chrome |
| `tabLabel` / `tabLabelActive` | `Inter_500Medium` / `Inter_600SemiBold` | 11 / 13     | sentence case, chrome            |

- **`AppText`** (`components/ui/AppText.tsx`): `<AppText variant="bookTitle" color="onSurfaceVariant">…</AppText>`. `variant` defaults to `body`, `color` (a theme colour role) to `onSurface`, read from `useTheme()`; a `style` prop is applied last. `allowFontScaling` is always on. **Dynamic Type cap:** chrome variants (`sectionLabel`, `tabLabel*`) set `maxFontSizeMultiplier` = `CHROME_MAX_FONT_SCALE` (1.3) because they sit in fixed bars; content variants are not capped.
- `fontFamilyHeadline` (`NotoSerif_700Bold`) and `fontFamilyBody` (`Inter_400Regular`) remain as aliases while screens move to variants. The `fontWeight*` tokens were removed.
- Guard: `__tests__/unit/noFontWeightWithCustomFont.test.ts` fails on a style object in `app/` or `components/` that sets `fontWeight` with `fontFamily`, on `fontWeight` in any file outside its `MIGRATION_PENDING` list (files owned by other #897 PRs; the list only shrinks), and checks `useFonts` loads exactly the typography font files.
- Size scale (older tokens): `fontSizeDisplay` (48) · `fontSizeH1` (40) · `fontSizeH2` (32) · `fontSizeH3` (24) · `fontSizeLG` (20) · `fontSizeBase` (16) · `fontSizeSM` (14)

#### Tab bar (#910)

`app/(tabs)/_layout.tsx`: labels always visible (`tabLabel`, 600 when active, no uppercase), the bar sizes to its content (`height: 'auto'`) plus the bottom safe-area inset, with a `minHeight` floor of top padding + React Navigation's tab-button padding (5 on each side) + 30 pill + 4 gap + the label line at the 1.3 cap + bottom padding + inset; never a fixed number. Root cause of the clipped labels (#910): `dev` had a fixed height (`Platform.OS === 'ios' ? 80 : 64`) with fixed vertical padding and 10px uppercase labels with no line height, so the label had no room; the first fix in this PR computed a height (65) that still missed React Navigation's 5px button padding. Active tab: `primary` icon inside a 56×30 `primarySoft` pill behind the icon only; inactive `onSurfaceVariant`. Bar `surfaceBar` with a hairline `outline` top border, same in both themes. Tests: `TabBarV2.test.tsx`.

#### Icons

- Library: **MaterialIcons** from `@expo/vector-icons`
- Tab icons: `document-scanner` (Scan) · `auto-stories` (Wishlist) · `library-books` (My Books) · `settings` (Settings)
- Header policy (#936): **no screen shows a Stack header.** `(tabs)`, `(auth)/login`, `trash` and `scan-queue` all set `headerShown: false` (for login, #908: with no `(auth)/_layout` the root Stack sees the route as `(auth)/login`, so a `(auth)` entry would match nothing). Each screen owns its top safe area with `useSafeAreaInsets().top` (the camera overlay on Scan; `LibraryScreen` for My Books / Wishlist; the container or list padding on Settings, Trash and the scan queue) and shows its own title; Trash and the scan queue keep their own in-content Back link. The `InnerStack` `screenOptions` stay opaque (`theme.colors.background`, no shadow, `headerTransparent: false`, #906) as a fallback for a screen added without its own header. Theme toggle (`ThemeToggleButton`) and help / feedback ("Send feedback" row, `settings-feedback`, opens the same sheet the old "?" did) live in Settings.
- Do **not** use Ionicons — it has been replaced

#### Shared sheets

- `components/InAppBanner.tsx` is a floating rounded card inset 16px, placed at safe-area top + 8 on every screen (no screen has a Stack header). Persistence (#909): every banner clears on a route change (`usePathname`); an `error` banner with actions stays until acted on or dismissed; any banner with actions also stays while a screen reader is on (`AccessibilityInfo`; iOS and Android only, because react-native-web always reports `true`); otherwise banners with actions time out after `duration` or 10 s, others after `duration` or 5 s.
- `components/SheetModal.tsx` is the shared sheet scaffold (#903), used by `BookCandidatePicker`, `AddUnverifiedBookSheet` and `library/BookDetailSheet`. Anatomy, top to bottom: `scrim` backdrop, a 36x5 grabber (`outline`), a header (serif `sheetTitle` title with the header role, optional `subtitle`, and a text `Button` close action with `closeLabel` / `closeA11yLabel`, both translated by the caller), a body wrapper (`flexShrink: 1`; callers put their own `ScrollView` inside it), and a footer pinned below it (top hairline, `surfaceBar` background, bottom padding = 16 + safe-area inset) that holds the primary `Button`(s). The title takes screen-reader focus when the sheet opens. Pass the body as children and the actions as `footer`. Keep `flexShrink: 1` on the sheet and body, never `flex: 1` alone in a content-sized parent. `onHidden` is the Modal's `onDismiss` and is unchanged, so `useModalHandoff` in `GlobalScanModals` keeps working. The sheet is still a `pageSheet` Modal; gesture dismissal is not part of it (phase 7).
- `FeedbackWidget` is not on `SheetModal` yet: it is a transparent bottom sheet with a `KeyboardAvoidingView` and its own `flexShrink` rules, so moving it would change behaviour.

#### BookCover (#902)

- `components/ui/BookCover.tsx` draws every book cover in a fixed 2:3 frame; never hand-roll a cover `Image`. Props: `title`, `coverUrl`, `size`, optional `accessibilityLabel`, `decorative`, `testID`, `style`.
- Trash (#932): `app/trash.tsx` draws its own compact top row below the inset (a 44px "Back" button calling `leaveTrash()`, a centred 17pt 600 "Trash" `header`), then an info card (`field` background, radius 12, decorative info icon, the 30-day rule) as the list header, then `LibraryRow`s without `onPress` (xs cover, `testID` `trash-item-<id>`) with a trailing `TonalChip` "Restore {{title}}" (`BoughtChip` is the same chip with a check icon). Empty and error states are `ui/EmptyState` (`trash-empty`, `trash-error`). The row's second line is `LibraryRow`'s `metaText`: "Deleted for good in N days" (`trash.purgeIn`, CLDR plurals) or "Deleted for good today", from `daysUntilPurge(purge_after)` in `lib/trash.ts`, which counts calendar days in the device time zone (0 on the purge day, DST-safe). At 3 days or fewer (`PURGE_SOON_DAYS`) the line is `danger` 600 (`metaTone="danger"`); the number is always in the text, so colour is never the only signal. A `LibraryRow` with no `onPress` and an `accessibilityLabel` reads its text block as one element (`trash.rowA11y`: title, author, days left). Covers are not desaturated (no cross-platform image filter).
- Sizes: `xs` 48x72 (library rows: `LibraryRow`) · `sm` 52x78 (candidate picker, Trash; all decorative) · `md` 104x156 (grids, large rows) · `lg` 136x204 (detail sheet). `md`/`lg` use `radius.coverLarge`, the rest `radius.cover`.
- With no `coverUrl`, or after the image's `onError`, it draws a typographic cover: `coverBlank` background, the title in Noto Serif (`fontFor('600','serif')`) in `onCoverBlank`, a thin inner frame, and a line clamp scaled to the size. A shadow and a spine shade (both from the `shadow` token) decorate the frame.
- Fallback title sizing (#924). `adjustsFontSizeToFit` is native-only, so `fallbackTitleStyle(title, size)` sizes the font deterministically from the widest unbreakable unit (a break comes after whitespace, `-`, en/em dash or `/`, and the separator stays on the line before, so the unit is e.g. `Wuthering-`), measured with real Noto Serif SemiBold advances (`theme/coverTextWidths.ts`: Basic Latin, Latin-1, Latin Extended-A and curly quotes; table generated by `scripts/gen-cover-widths.py`; unknown characters count 1em) plus a 4% margin. The font is the largest that fits the text box, capped at the size's normal font and floored at 6px (`FALLBACK_MIN_FONT`). If the widest unit does not fit at the floor (a long single word on a small cover), the cover is drawn as a plain placeholder with no title text, rather than splitting the word or showing initials (owner decision). The cover's label (`common:noCoverAvailable`, or the row's own label) still names the book. Han, Hiragana and Katakana characters each wrap alone (1em); every other run, Hangul included, is one unit, so a Latin word inside CJK text still counts whole, so they count 1em per character and keep the normal size. Line height stays proportional and `numberOfLines` still truncates by whole lines. These are estimates, so a font fallback can still differ slightly. The text box has an explicit width (frame minus the inner-frame inset and 1px on each side) and the fallback frame has no padding: text widths are xs 32 to 40, sm 36 to 42, md 88 unchanged, lg 120 to 114 (frame minus inset and 1px of air per side).
- Accessibility: by default the frame is an `image` with `common:coverAlt` (or `common:noCoverAvailable` for the generated cover). Pass `decorative` whenever the surrounding row already announces the title: LibraryRow (tappable or not), the candidate picker and Trash are all decorative; only the detail sheet keeps the image label. `decorative` sets `accessible={false}`, `importantForAccessibility`, `accessibilityElementsHidden` **and `aria-hidden`** (react-native-web ignores the native props, so without `aria-hidden` the web a11y tree still exposes the cover). The typeset fallback title is always `aria-hidden`, so it is not read twice.
- The bitmap is isolated in `CoverImage` (React Native `Image` for now) so it can be swapped for `expo-image` once #901 lands. `CoverImage`, `FallbackCover` and `BookCover` are memoised (stable `onError`), so a list of covers does not re-render on unrelated updates. It is not yet exported from a `components/ui` index.

#### Library screens (My Books and Wishlist)

- Both screens are built from `components/library/`: `LibraryRow` (+ `LibraryRowSeparator`), `BookActionsSheet` (My Books ⋯ menu), `BoughtChip` (Wishlist), `LibraryHeader`, `LibraryScreen` (the full-height themed container, also used for the loading state, so the two screens do not duplicate that markup), `StatusTabs`, `LibrarySearchBar`, `EmptyState`, `BookDetailSheet` (both screens). `libraryShared.ts` is the single source of `SCREEN_PADDING`, `NEXT_STATUS`, `STATUS_TAB_KEYS`, `STATUS_COLOR_KEY`, `ROW_DIVIDER_INSET` and the `LibraryBook` type; `useLibraryAccent()` gives the accent colours (`primary` / `onPrimary` in both modes).
- `LibraryRow` (#929, memoised): an edge-to-edge list row, at least 96px tall (grows with Dynamic Type): decorative `BookCover` `xs` (48x72), 14px gap, then the text column (`bookTitle` serif, 1 line; author in `secondary`/`onSurfaceVariant`; a `meta` line with an 8px status dot, the translated status (`my-books:statusTab.*`, the same string as the tab chips) and "· year"; the Unverified badge) and an optional `trailing` slot. Props: `item`, `onPress?`, `trailing?`, `accessibilityLabel`, `accessibilityHint?`, `showStatus?`, `testID?`. The row owns no actions: the open-detail `Pressable` is an absolute-fill overlay _sibling_ of the content (#907), the cover/text are `pointerEvents="none"` and hidden from assistive tech, and `trailing` is a sibling of the overlay. The hairline between rows is `LibraryRowSeparator` (`ItemSeparatorComponent`, inset to the text column, so the last row has none). The body label is `{{title}} — {{status}}, by {{author}}` with the **translated** status (never the raw enum).
- My Books trailing slot: an `IconButton` (more, 44x44) opening `BookActionsSheet` (`SheetModal` + `ListSection`/`ListRow`: Mark as next, View details, Remove from library). On iOS "View details" waits for the menu's `onHidden` before opening the detail sheet; elsewhere it opens at once. Wishlist trailing slot: the tonal `BoughtChip` (32px visual, 44px target; disabled while offline or mutating); tapping the row opens `BookDetailSheet`, which has Remove (the visible remove path without swipe; swipe is S2). Rows are memo wrappers (`MyBookRow`, `WishlistRow`) with callbacks stable by id; both FlatLists use `initialNumToRender={12}` and `removeClippedSubviews` on Android. `StatusTabs` is a fixed-height (`STATUS_TABS_HEIGHT`) `flexGrow: 0` ScrollView so it never collapses or stretches.
- `hooks/useLibraryMutations.ts` owns advance-status and remove (the per-row in-flight guard, offline check, error alerts, the "moved to trash" banner). Each screen passes `onAdvanced` / `onRemoved` to update its own list, so My Books and Wishlist keep their different list rules. Put new shared row/header behaviour there, not in the screens.

#### Logo / App Icon

- Source asset: `frontend/assets/logo.png` (chihuahua + open book on navy, 1024×1024)
- Configured in `app.json` as iOS icon, Android adaptive foreground (`backgroundColor: #0f426f`), and web favicon
- No longer rendered in-app: the header wordmark was removed in #936

### Default tab ("Open on", #904, #913)

- The user's "Open on" screen is stored on the server (`users.default_tab`: `scan`, `wishlist`, `my-books` or `null`), never on the device. `lib/defaultTab.ts` is the single source of truth: `DEFAULT_TABS`, `parseDefaultTab` / `defaultTabRoute` (anything unknown, `null` or missing is Scan), `defaultTabFromMe` (reads a `GET /auth/me` body), `loadDefaultTab` (`null` on failure or after `DEFAULT_TAB_FETCH_TIMEOUT_MS`, 3 s; never rejects) and `saveDefaultTab` (`PATCH /users/me/preferences`).
- **Launch** never makes an extra call or waits longer: `AuthContext`'s existing session check (`GET /auth/me`, `SESSION_CHECK_TIMEOUT_MS`) also reads `default_tab`. Only a `valid` session uses it; `unknown` (offline, 5xx, 403, timeout, a 401 whose refresh failed transiently) and `dead` leave it `null` and land on Scan, and the session-check behaviour (#722) is unchanged.
- **Sign-in** (`login` / `testLogin` -> `storeSession`) calls `loadDefaultTab()` after the tokens are stored and the request gate is open, waiting at most 3 s, then signs in; a failure or timeout leaves it `null` and lands on Scan. A sign-out during that wait wins (`sessionGenRef`), so the late answer never signs the user back in.
- `AuthContext` exposes `defaultTab`: the tab read at launch or sign-in, or `null` when it could not be read (and after sign-out). `null` routes to Scan. A server `null` or unknown value is read as `'scan'` (known), not `null`. `app/index.tsx` redirects to `defaultTabRoute(defaultTab)`; the auth-group redirect effect does the same.
- **Settings row** (`components/OpenOnSetting.tsx`, mounted in `app/(tabs)/settings.tsx` below the theme toggle): shows the current choice and opens a transparent modal picker (radio group: Scan, Wishlist, My Books; Cancel and the backdrop close it). `hooks/useDefaultTabSetting.ts` keeps its own copy of the value, starting from `AuthContext.defaultTab`; only when that is `null` (launch was offline, the read failed) does it re-read it once on mount with `loadDefaultTab`, and a late re-read never overwrites a choice. AuthContext's copy is not updated by a save: the app routes on it only at launch and sign-in, which both read it fresh, and tab screens stay mounted for the whole signed-in session. Picking is optimistic; on failure the old value comes back, a `settings` breadcrumb records `status` / `code` from `toApiError`, and `useBanner` shows `settings:openOnSaveFailed`. Copy is in the `settings` namespace (`openOn*` keys) in all 13 locales and `_meta`. Tests: `defaultTab.test.ts`, `AuthContext.test.tsx` ("Open on" tab), `IndexScreen.test.tsx`, `OpenOnSetting.test.tsx`.

### Feedback flow

- Entry points: the "Send feedback" row in Settings (the header "?" button was removed with the top bar, #936). It calls `useFeedback().openFeedback()` from `FeedbackProvider` (`components/FeedbackWidget/FeedbackContext.tsx`, mounted in `app/_layout.tsx`), which owns the single bottom-sheet `FeedbackWidget` and the `FeedbackThanksBanner`.
- Sheet: Bug / Feature request chips + description only (max 2000). On success it closes itself and the banner (4 s, non-interactive, at the top of the screen below the status bar) confirms.
- Submission goes to Sentry User Feedback (`captureFeedback`, `source: 'in_app_feedback'`, tag `feedback.type`); session logs are attached for bug reports only (`SessionLogger`: 200 lines, each cut at 1,000 chars, 100 KB total, redacted before the cut: Authorization/Bearer/JWT/email/token params; never throws). Throttled per install to 5 submissions / 10 min.
- Sheet colours come from `useTheme()`; the backdrop uses the `scrim` token (visible on Android and web through the 24px top gap; iOS `pageSheet` draws its own). The footer bar is omitted when `footer` is empty, so a closing sheet shows no empty bar. Keep the sheet/body `flexShrink: 1` (not `flex: 1`) or the body collapses to zero height and the sheet renders blank.
- There is no floating button; do not add one over the tab bar.

### UI snapshots (before/after for UI PRs)

`scripts/ui-snapshots/capture.mjs` drives the Expo web dev server at iPhone size (390x844 @2x) against a mocked API (`fixtures.mjs`: public-domain titles, generated SVG covers) and saves every screen and main state in light and dark to `ui-snapshots/current/` (gitignored), plus `console-errors.txt`. Sign-in uses the `__DEV__` "Test Login" button, intercepted by the mock; web keeps tokens in memory, so the run navigates by tapping, never by reloading. Run it on `dev` and on your branch (`--out ui-snapshots/before` / `after`) and attach the pairs to UI PRs.

```bash
cd frontend
EXPO_PUBLIC_API_URL=http://mock.api EXPO_PUBLIC_GOOGLE_WEB_CLIENT_ID=snapshot npx expo start --web --port 8081
```

```bash
cd frontend
node scripts/ui-snapshots/capture.mjs --out ui-snapshots/current
```

Add `--video` to also record one walkthrough per theme (`<out>/<theme>-walkthrough.mp4`; Chromium screencast frames encoded by `ffmpeg`, which must be on PATH), holding about 1.4 s on each screen and showing taps; `--themes light` limits the run. Other flags: `--url` (default `http://localhost:8081`; only `localhost`/`127.0.0.1` are accepted). Every HTTP(S) request and WebSocket other than the dev server and the mock API is blocked for the whole browser context (popups and service workers included), so a stray `EXPO_PUBLIC_API_URL` or Sentry DSN never reaches a real service; blocked origins are listed in the output. Each run first deletes `<out>/<theme>/` and its walkthrough (so `--out` must be inside the current folder or the temp folder, and `--themes` only takes `light`/`dark`), and the run exits 1 with a list when any step or video encode fails, so a partial capture never passes for a complete one. `npm run lint` doesn't cover `.mjs`, so lint the script directly (`npx eslint --no-ignore scripts/ui-snapshots/capture.mjs`). Invalid arguments exit 2. `playwright-core` needs a Chromium: run `npx playwright install chromium`, or set `CHROMIUM_PATH` to an already-installed one (cloud sessions: `/opt/pw-browsers/chromium`). Selectors are accessibility labels and `testID`s, so keep those stable when redesigning; when a screen's labels change, update the script in the same PR. Not covered: the native camera view, the scan queue, the add-it-yourself form, offline/quota notices.

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

| Namespace    | Covers                                                      |
| ------------ | ----------------------------------------------------------- |
| `common`     | Error title, Close, Remove, cover alt                       |
| `auth`       | Login screen                                                |
| `tabs`       | Tab bar labels + a11y labels                                |
| `my-books`   | My Books screen — status tabs, badge, detail sheet, actions |
| `scan`       | Scan screen — alerts, mode tabs, permission, camera         |
| `settings`   | Settings screen                                             |
| `wishlist`   | Wishlist screen                                             |
| `components` | `BookCandidatePicker`, `ThemeToggleButton`                  |
| `feedback`   | In-app feedback widget                                      |

#### Using translations in a screen

```tsx
import { useTranslation } from "react-i18next";

const { t } = useTranslation("my-books");
// Cross-namespace lookup:
t("close", { ns: "common" });
// Interpolation:
t("coverAlt", { ns: "common", title: book.title });
// Dynamic nested key:
t(`status.${item.status}`);
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

`lib/scanErrors.ts` is the only place that knows the `POST /scan` refusal codes. `classifyScanError(err)` maps an axios error to exactly one UI state (`daily_limit`, `requests_daily_limit`, `in_progress`, `paused`, `unavailable`, `enhanced_refused`, `generic`; the table is in the file header); it distinguishes by `response.data.detail`, not status alone (429 is `daily_scan_limit`, `scan_in_progress` and `enhanced_throttled`). There is no monthly limit (#582): no `monthly_limit` state, notice or copy; a 402 from an older server (`scan_limit_reached` / `enhanced_limit_reached`) is `generic`. `enhanced_refused` (#533: `enhanced_requires_premium`, `enhanced_throttled`) has no notice card: `ScanJobContext` shows a banner (`enhancedRefusalMessage`), falls back to the standard results and stops only enhanced photos in the drain. The enhanced re-scan is offered only when `enhancedScansRemaining` (from `enhanced_scans_remaining` on scan responses: the Premium daily allowance left since #582) is above 0. `toScanNotice` / `scanNoticeMessage` turn a state into the notice the scan screen shows. `ScanJobContext` owns the `scanNotice` and `usageRefreshKey`; the upload itself is `executeScan(deps, job)` in `lib/scan/executeScan.ts` (the provider rebuilds its typed `ScanExecDeps`, `lib/scan/types.ts`, in an effect and assigns `executeScanRef`; refs are read at call time), with the refusal / retry-schedule / enhanced-refusal handlers and error-classification helpers in `lib/scan/scanOutcomes.ts`. The provider stays a thin owner of state, refs, persistence and sign-out; two hooks hold the rest (#805). `hooks/useScanQueue.ts` is the queue: `drainQueue` (one drain at a time via `drainingRef` / `drainAgainRef`, narrowed by `stopDrainRef`), `resumeQueue`, the reconnect drain, the retry-schedule timer (`nextAttemptAt`, cleared on unmount), and `startScan` / `retryScan` / `restartScanSchedule`, with `MAX_RETRIES` and the drain/timer constants. `hooks/useScanReview.ts` is the review: `handleAddBooks` and the "Add it yourself" drafts (`addYourself`, `unverifiedSaved`, `dismissUnverified`, and reopening the review when the forms close). Refs that the sign-out effect or `executeScan` also touch (`jobsRef`, `inFlightRef`, `generationRef`, the drain refs, `draftSourcesRef`, `reopenReviewJobIdRef`) and the `unverifiedDrafts` state stay in the provider and are passed in, so the hooks add no state and their callbacks keep stable identities (the memoised context value re-renders consumers no more than before; `ScanJobContextRenderCount.test.tsx`). The silent `scan_in_progress` retry (max 3, honouring `Retry-After`) also lives in `executeScan`. `hooks/useScanUsage.ts` fetches `GET /scan/usage` for `components/ScanStatusPanel.tsx` (scan screen) and the Settings row, and returns `null` offline or on failure so the counter just hides. Two counters (#577): `remainingToday` (found, falling back to `used_today` / `daily_limit` from an older server) and `noMatchRemainingToday` (`null`, so the line hides, when the server sends no `no_match_*`). The no-match banner copy comes from `noMatchMessage` (`lib/scanJob.ts`) on the scan response's `no_match`. `unavailable` (503 `scan_unavailable`, #604; also the Turnstile refusals `400 "Missing Turnstile token"` / `403 "Turnstile verification failed"`, codes `turnstile_missing` / `turnstile_failed` in `TURNSTILE_REFUSALS`, #740) is the only retryable state: `ScanJobContext` retries the photo on `SCAN_RETRY_DELAYS_MS` (in `scanErrors.ts`, the one place for the delays) and stores `attempts` / `nextAttemptAt` on the job. Add new backend codes in `scanErrors.ts` and its table-driven test, never inline in a component.

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

### Dependencies: vendored `decode-uri-component` (#779)

`expo-router` -> `query-string@7.1.3` -> `decode-uri-component@^0.2.2` is affected by CVE-2026-118664 (DoS via quadratic replacement of percent-encoded runs; fixed in 0.5.1). The fixed releases (>= 0.3) are ESM-only while `query-string@7` `require()`s the package, and expo-router (SDK 56-58) keeps `query-string@^7.1.3`, and no patched 7.x exists, so a plain override would break Metro and Jest. Instead `frontend/vendor/decode-uri-component/` holds a CommonJS port of the 0.5.1 algorithm (MIT, licence file kept; package version `0.5.1+bookshelf.0`, which semver compares equal to 0.5.1, so Trivy no longer matches the CVE range). `package.json` lists it as `"decode-uri-component": "file:./vendor/decode-uri-component"` and the `overrides` entry `"$decode-uri-component"` forces every consumer onto it (a `file:` path directly in `overrides` resolves relative to the dependent and breaks). No `patch-package` or `postinstall`. Expo SDK 56, 57 and 58 all pin `query-string@^7.1.3` (verified with `npm view`). **Removal trigger:** on each Expo SDK bump, run `npm view expo-router@<sdk> dependencies.query-string`; once it is `>=8` (or `query-string` no longer depends on `decode-uri-component <0.5.1`), delete `vendor/decode-uri-component/`, the `dependencies` entry and the `overrides` entry, then re-run `npm install`. `vendor/` is excluded from Prettier and ESLint so the file stays comparable with upstream. Tests: `__tests__/unit/QueryStringDecode.test.ts`, `__tests__/unit/RouterParamParsing.test.ts`.

### Core UI components (`components/ui/`, #900)

Shared, themed building blocks for the v2 design. Import from `components/ui` (the index). All colours come from `useTheme()`, all text from `AppText` variants (no `fontWeight`), and every component takes an optional `testID`. **No copy lives inside a component**: callers pass translated strings (labels, placeholders, accessibility labels). Screens migrate to these in later phases; the older `components/library/EmptyState.tsx` and `LibrarySearchBar.tsx` stay until then. Icons are `MaterialIcons` names.

| Component                 | Use it for                                                                                                                                                                                                                                                                                                                             |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Button`                  | Any text action. `variant`: `primary` (default; 52px, radius 12, `primary`/`onPrimary`), `secondary` (tonal `primarySoft`/`primary`), `text` (no fill, 44px min), `destructive` (`danger`/`onDanger`). Props: `label`, `onPress`, `disabled`, `loading` (spinner, blocks presses, announces `busy`), optional leading `icon`.          |
| `IconButton`              | Icon-only action, 44x44. `accessibilityLabel` is required by the prop type.                                                                                                                                                                                                                                                            |
| `Chip`                    | Filter chip with optional `count`. `selected` = `primary` fill + `onPrimary` text; unselected = transparent with an `outlineField` border (WCAG 1.4.11). Exposes `accessibilityState.selected`.                                                                                                                                        |
| `SearchField`             | 40px visual field (radius 10, `field` fill, 1px `outlineField` border, WCAG 1.4.11) inside a 44px wrapper that owns a 44x44 clear button (shown when `value` is not empty; refocuses the input). Needs `placeholder`, `accessibilityLabel`, `clearAccessibilityLabel`. Controlled (`value`/`onChangeText`).                            |
| `ListSection` + `ListRow` | Grouped inset list (settings, details). `ListSection` takes an optional `title` and puts inset dividers between its `ListRow` children. A row is at least 52px: `icon` tile, `label`, optional `hint` and `value`, chevron (default on when `onPress` is set), `destructive` variant. Without `onPress` a row is static, not a button. |
| `SectionLabel`            | The `sectionLabel` text style (uppercase, chrome font-scale cap), announced as a header.                                                                                                                                                                                                                                               |
| `EmptyState`              | Empty or first-run state: `illustration` slot (decorative, hidden from screen readers), serif `title`, `body`, `primaryAction` and `secondaryAction` (`{ label, onPress, testID? }`).                                                                                                                                                  |
| `ProgressBar`             | Determinate bar. `value`/`min`/`max` (clamped), `accessibilityRole="progressbar"` with `accessibilityValue`. Needs `accessibilityLabel`.                                                                                                                                                                                               |

Tests: `__tests__/unit/ui*.test.tsx` (one per component).

## Lint

Lint is warning-free (#817) and `npm run lint` runs `eslint . --max-warnings 0`. In `frontend/.eslintrc.js`, `import/first`, `react/display-name`, `react-hooks/exhaustive-deps`, `@typescript-eslint/no-unused-vars` and `@typescript-eslint/no-require-imports` are errors.

- `no-require-imports` is off only for `__tests__/**` and `jest.setup.ts` (one `overrides` block): `jest.mock()` factories, `jest.isolateModules` and dynamic locale JSON loads need a lazy `require()` because Jest hoists mocks above imports. App code may not `require`.
- `import/first` has no exceptions. `app/_layout.tsx` keeps `../lib/sentry` as the first import (rule 13) and runs `SessionLogger.init()` and the lifecycle breadcrumb below the imports (ES imports are hoisted, so Sentry still loads first; `__tests__/unit/LayoutInitOrder.test.tsx` checks the source structure: Sentry is the first import and the init code comes after the last import).
- A new `eslint-disable` needs a reason on the same line (`// eslint-disable-next-line <rule> -- <why>`). Fix the code first; do not add one to silence a warning.

## Commands

```bash
npx jest                          # unit tests
npx jest --coverage               # with coverage report
npx eslint .                      # lint (includes a11y rules; must be warning-free)
npx prettier --check .            # format check
npx prettier --write .            # format fix
```

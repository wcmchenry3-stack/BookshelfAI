// Design tokens v2 (#898): the approved literary palette, spacing and radii.
//
// `palette` holds the v2 roles with their exact approved values. `tokens.colors`
// exposes every role as { light, dark }: first the v2 roles, then the older MD3
// roles and convenience aliases, which are temporary mappings onto the v2
// palette so screens that have not moved yet still compile and pick up the new
// colours. A follow-up removes the aliases once no caller is left.
//
// One accent hue in both modes: `primary`. There is no second (gold) accent;
// `tertiary*` now map onto the primary accent.
import { typography } from './typography';

const light = {
  background: '#F6F4EF',
  surface: '#FFFFFF',
  surfaceBar: '#FBFAF7',
  field: '#ECE8DF',
  onSurface: '#1A1C20',
  onSurfaceVariant: '#5E636B',
  outline: '#E4DFD4',
  outlineCard: '#ECE7DC',
  outlineChip: '#DAD4C7',
  primary: '#0F426F',
  onPrimary: '#FFFFFF',
  primarySoft: '#DCE6F1',
  danger: '#B3261E',
  onDanger: '#FFFFFF',
  dangerSoft: '#F7E3DF',
  success: '#2F7A4F',
  onSuccess: '#FFFFFF',
  statusWishlist: '#A56A12',
  statusPurchased: '#5F6F86',
  statusReading: '#0F426F',
  statusRead: '#2F7A4F',
  coverBlank: '#E7E1D3',
  onCoverBlank: '#4A4740',
} as const;

// Dark values not in the approved list (#898 gives none for them), chosen here
// and flagged for owner review: outlineCard (= outline), dangerSoft, onDanger,
// onSuccess.
const dark: Record<keyof typeof light, string> = {
  background: '#121417',
  surface: '#1B1E23',
  surfaceBar: '#16181C',
  field: '#1F2227',
  onSurface: '#ECE9E3',
  onSurfaceVariant: '#A4A8AF',
  outline: '#2A2D33',
  outlineCard: '#2A2D33',
  outlineChip: '#33363C',
  primary: '#A9C9EE',
  onPrimary: '#0B2540',
  primarySoft: '#22344A',
  danger: '#FFB4AB',
  onDanger: '#690005',
  dangerSoft: '#3A2220',
  success: '#7FC59B',
  onSuccess: '#10291B',
  statusWishlist: '#E0B062',
  statusPurchased: '#A3B1C6',
  statusReading: '#A9C9EE',
  statusRead: '#7FC59B',
  coverBlank: '#2A2C30',
  onCoverBlank: '#C9C4BA',
};

export const palette = { light, dark } as const;

type Role = keyof typeof light;
/** `{ light, dark }` for a v2 role. */
const role = (r: Role) => ({ light: light[r], dark: dark[r] });

export const tokens = {
  colors: {
    // ── v2 roles ───────────────────────────────────────────────────────────
    background: role('background'),
    surface: role('surface'),
    surfaceBar: role('surfaceBar'),
    field: role('field'),
    onSurface: role('onSurface'),
    onSurfaceVariant: role('onSurfaceVariant'),
    outline: role('outline'),
    outlineCard: role('outlineCard'),
    outlineChip: role('outlineChip'),
    primary: role('primary'),
    onPrimary: role('onPrimary'),
    primarySoft: role('primarySoft'),
    danger: role('danger'),
    onDanger: role('onDanger'),
    dangerSoft: role('dangerSoft'),
    success: role('success'),
    onSuccess: role('onSuccess'),
    statusWishlist: role('statusWishlist'),
    statusPurchased: role('statusPurchased'),
    statusReading: role('statusReading'),
    statusRead: role('statusRead'),
    coverBlank: role('coverBlank'),
    onCoverBlank: role('onCoverBlank'),

    // ── MD3 roles: temporary mappings onto the v2 palette ─────────────────
    primaryContainer: role('primarySoft'),
    onPrimaryContainer: role('primary'),
    primaryFixed: role('primarySoft'),
    primaryFixedDim: role('primarySoft'),
    onPrimaryFixed: role('primary'),
    onPrimaryFixedVariant: role('primary'),
    inversePrimary: { light: dark.primary, dark: light.primary },

    secondary: role('onSurfaceVariant'),
    onSecondary: role('surface'),
    secondaryContainer: role('field'),
    onSecondaryContainer: role('onSurface'),
    secondaryFixed: role('field'),
    secondaryFixedDim: role('field'),
    onSecondaryFixed: role('onSurface'),
    onSecondaryFixedVariant: role('onSurfaceVariant'),

    // Single accent: tertiary (the old dark-mode gold) maps onto primary.
    tertiary: role('primary'),
    onTertiary: role('onPrimary'),
    tertiaryContainer: role('primarySoft'),
    onTertiaryContainer: role('primary'),
    // UnverifiedBadge background/text: the soft accent chip.
    tertiaryFixed: role('primarySoft'),
    tertiaryFixedDim: role('primarySoft'),
    onTertiaryFixed: role('primary'),
    onTertiaryFixedVariant: role('primary'),

    error: role('danger'),
    onError: role('onDanger'),
    errorContainer: role('dangerSoft'),
    onErrorContainer: role('danger'),

    surfaceVariant: role('field'),
    surfaceContainerLowest: role('surface'),
    surfaceContainerLow: role('surface'),
    surfaceContainer: role('surfaceBar'),
    surfaceContainerHigh: role('field'),
    surfaceContainerHighest: role('field'),
    surfaceBright: role('surface'),
    surfaceDim: role('field'),
    inverseSurface: { light: light.onSurface, dark: dark.onSurface },
    inverseOnSurface: { light: light.background, dark: dark.background },
    surfaceTint: role('primary'),
    // Modal backdrop behind bottom sheets (8-digit hex: ~55% black).
    scrim: { light: '#0000008c', dark: '#0000008c' },

    outlineVariant: role('outlineCard'),

    // ── Translucent / fixed-value tokens, redefined per mode on v2 ────────
    // headerOverlay: dark frosted-glass stack header (background at 70%);
    // the light header stays transparent.
    headerOverlay: { light: 'transparent', dark: 'rgba(18,20,23,0.7)' },
    // tabBarOverlay: frosted tab bar (surfaceBar; 85% in dark).
    tabBarOverlay: { light: light.surfaceBar, dark: 'rgba(22,24,28,0.85)' },
    // outlineVariantFaint: outline at 40% in dark; none in light.
    outlineVariantFaint: { light: 'transparent', dark: 'rgba(42,45,51,0.4)' },
    shadow: { light: '#000', dark: '#000' },

    // ── ErrorBoundary surface ──────────────────────────────────────────────
    // ErrorBoundary reads the theme through ThemeContext.Consumer (falling
    // back to lightTheme), so these follow the v2 palette per mode.
    errorBoundarySurface: role('background'),
    errorBoundaryTitle: role('onSurface'),
    errorBoundaryBody: role('onSurfaceVariant'),
    errorBoundaryDetail: role('danger'),
    errorBoundaryDetailSurface: role('dangerSoft'),
    errorBoundaryAction: role('primary'),
    errorBoundaryOnAction: role('onPrimary'),

    // ── Convenience aliases (backward-compat) ──────────────────────────────
    text: role('onSurface'),
    textSecondary: role('onSurfaceVariant'),
    border: role('outline'),
    iconActive: role('primary'),
    iconInactive: role('onSurfaceVariant'),
  },

  typography,

  // 4 / 8 / 12 / 16 / 24 / 32 / 48. `ms` (12) sits between sm and md.
  spacing: {
    xs: 4,
    sm: 8,
    ms: 12,
    md: 16,
    lg: 24,
    xl: 32,
    xxl: 48,
  },

  radius: {
    // v2 component radii
    cover: 3,
    coverLarge: 5,
    field: 10,
    button: 12,
    card: 14,
    sheet: 16,
    chip: 9999,
    pill: 9999,
    // Older scale, kept while screens move to the component radii
    sm: 4,
    md: 8,
    lg: 12,
    xl: 16,
    full: 9999,
  },

  touchTarget: {
    min: 44, // WCAG 2.5.8
  },
} as const;

// MD3 color role tokens — light and dark values.
// Convenience aliases (background, text, textSecondary, border, iconActive,
// iconInactive, success) are kept so existing components compile unchanged
// while screens are migrated to the new roles one at a time.
export const tokens = {
  colors: {
    // ── Primary ────────────────────────────────────────────────────────────
    primary: { light: '#0f426f', dark: '#bdc7db' },
    onPrimary: { light: '#ffffff', dark: '#273140' },
    primaryContainer: { light: '#2e5a88', dark: '#101a29' },
    onPrimaryContainer: { light: '#aed1ff', dark: '#798395' },
    primaryFixed: { light: '#d2e4ff', dark: '#d2e4ff' },
    primaryFixedDim: { light: '#a0cafe', dark: '#a0cafe' },
    onPrimaryFixed: { light: '#001d36', dark: '#001d36' },
    onPrimaryFixedVariant: { light: '#194976', dark: '#194976' },
    inversePrimary: { light: '#a0cafe', dark: '#555f70' },

    // ── Secondary ──────────────────────────────────────────────────────────
    secondary: { light: '#47645d', dark: '#b5c8df' },
    onSecondary: { light: '#ffffff', dark: '#203243' },
    secondaryContainer: { light: '#c6e7dd', dark: '#36485b' },
    onSecondaryContainer: { light: '#4b6861', dark: '#a4b7cd' },
    secondaryFixed: { light: '#c9e9e0', dark: '#c9e9e0' },
    secondaryFixedDim: { light: '#adcdc4', dark: '#adcdc4' },
    onSecondaryFixed: { light: '#02201a', dark: '#02201a' },
    onSecondaryFixedVariant: { light: '#2f4c45', dark: '#2f4c45' },

    // ── Tertiary ───────────────────────────────────────────────────────────
    tertiary: { light: '#573c00', dark: '#fbbc00' },
    onTertiary: { light: '#ffffff', dark: '#402d00' },
    tertiaryContainer: { light: '#745202', dark: '#231800' },
    onTertiaryContainer: { light: '#f7c872', dark: '#a67c00' },
    tertiaryFixed: { light: '#ffdea8', dark: '#ffdea8' },
    tertiaryFixedDim: { light: '#eebf6b', dark: '#eebf6b' },
    onTertiaryFixed: { light: '#271900', dark: '#271900' },
    onTertiaryFixedVariant: { light: '#5e4200', dark: '#5e4200' },

    // ── Error ──────────────────────────────────────────────────────────────
    error: { light: '#ba1a1a', dark: '#ffb4ab' },
    onError: { light: '#ffffff', dark: '#690005' },
    errorContainer: { light: '#ffdad6', dark: '#93000a' },
    onErrorContainer: { light: '#93000a', dark: '#ffdad6' },

    // ── Surface ────────────────────────────────────────────────────────────
    surface: { light: '#edf2f8', dark: '#111317' },
    onSurface: { light: '#1b1c1a', dark: '#e2e2e8' },
    surfaceVariant: { light: '#e4e2de', dark: '#43483f' },
    onSurfaceVariant: { light: '#42474f', dark: '#c5c6cc' },
    surfaceContainerLowest: { light: '#ffffff', dark: '#0c0e12' },
    surfaceContainerLow: { light: '#f5f3ef', dark: '#1a1c20' },
    surfaceContainer: { light: '#efeeea', dark: '#1e2024' },
    surfaceContainerHigh: { light: '#eae8e4', dark: '#282a2e' },
    surfaceContainerHighest: { light: '#e4e2de', dark: '#333539' },
    surfaceBright: { light: '#edf2f8', dark: '#37393e' },
    surfaceDim: { light: '#dbdad6', dark: '#111317' },
    inverseSurface: { light: '#30312e', dark: '#e2e2e8' },
    inverseOnSurface: { light: '#f2f0ed', dark: '#2f3035' },
    surfaceTint: { light: '#35618f', dark: '#bdc7db' },
    // Modal backdrop behind bottom sheets (8-digit hex: ~55% black).
    scrim: { light: '#0000008c', dark: '#0000008c' },

    // ── Outline ────────────────────────────────────────────────────────────
    outline: { light: '#737780', dark: '#8f9096' },
    outlineVariant: { light: '#c2c7d0', dark: '#45474c' },
    // Translucent / fixed-value variants (values identical to the literals they replaced).
    // headerOverlay: dark frosted-glass stack header; light header stays transparent.
    headerOverlay: { light: 'transparent', dark: 'rgba(17,19,23,0.7)' },
    // tabBarOverlay: dark frosted-glass tab bar; light uses surfaceContainerLow's value.
    tabBarOverlay: { light: '#f5f3ef', dark: 'rgba(17,19,23,0.85)' },
    // outlineVariantFaint: outlineVariant (#45474c) at ~40% opacity; none in light.
    outlineVariantFaint: { light: 'transparent', dark: 'rgba(69,71,76,0.4)' },
    shadow: { light: '#000', dark: '#000' },

    // ── ErrorBoundary surface ──────────────────────────────────────────────
    // The boundary mounts ABOVE ThemeProvider, so it has always rendered one
    // fixed palette in both modes. Same values in light and dark = no visual change.
    errorBoundarySurface: { light: '#fff', dark: '#fff' },
    errorBoundaryTitle: { light: '#111', dark: '#111' },
    errorBoundaryBody: { light: '#555', dark: '#555' },
    errorBoundaryDetail: { light: '#c00', dark: '#c00' },
    errorBoundaryDetailSurface: { light: '#fff0f0', dark: '#fff0f0' },
    errorBoundaryAction: { light: '#007AFF', dark: '#007AFF' },
    errorBoundaryOnAction: { light: '#fff', dark: '#fff' },

    // ── Convenience aliases (backward-compat) ──────────────────────────────
    background: { light: '#edf2f8', dark: '#111317' },
    text: { light: '#1b1c1a', dark: '#e2e2e8' },
    textSecondary: { light: '#42474f', dark: '#c5c6cc' },
    border: { light: '#c2c7d0', dark: '#45474c' },
    success: { light: '#16A34A', dark: '#4ADE80' },
    onSuccess: { light: '#ffffff', dark: '#052e16' },
    iconActive: { light: '#0f426f', dark: '#bdc7db' },
    iconInactive: { light: '#47645d', dark: '#b5c8df' },
  },

  typography: {
    // Font families
    fontFamilyHeadline: 'NotoSerif_700Bold' as const,
    fontFamilyBody: 'Inter_400Regular' as const,

    // Scale
    fontSizeDisplay: 48,
    fontSizeH1: 40,
    fontSizeH2: 32,
    fontSizeH3: 24,
    fontSizeXL: 24, // kept for compat
    fontSizeLG: 20,
    fontSizeBase: 16,
    fontSizeSM: 14,
    fontSizeXS: 12,

    lineHeightBody: 1.5, // WCAG 1.4.12
    fontWeightNormal: '400' as const,
    fontWeightMedium: '500' as const,
    fontWeightSemiBold: '600' as const,
    fontWeightBold: '700' as const,
  },

  spacing: {
    xs: 4,
    sm: 8,
    md: 16,
    lg: 24,
    xl: 32,
    xxl: 48,
  },

  radius: {
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

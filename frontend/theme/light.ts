import { tokens } from './tokens';

const c = tokens.colors;

export const lightTheme = {
  colors: {
    // MD3 roles
    primary: c.primary.light,
    onPrimary: c.onPrimary.light,
    primaryContainer: c.primaryContainer.light,
    onPrimaryContainer: c.onPrimaryContainer.light,
    primaryFixed: c.primaryFixed.light,
    primaryFixedDim: c.primaryFixedDim.light,
    onPrimaryFixed: c.onPrimaryFixed.light,
    onPrimaryFixedVariant: c.onPrimaryFixedVariant.light,
    inversePrimary: c.inversePrimary.light,

    secondary: c.secondary.light,
    onSecondary: c.onSecondary.light,
    secondaryContainer: c.secondaryContainer.light,
    onSecondaryContainer: c.onSecondaryContainer.light,
    secondaryFixed: c.secondaryFixed.light,
    secondaryFixedDim: c.secondaryFixedDim.light,
    onSecondaryFixed: c.onSecondaryFixed.light,
    onSecondaryFixedVariant: c.onSecondaryFixedVariant.light,

    tertiary: c.tertiary.light,
    onTertiary: c.onTertiary.light,
    tertiaryContainer: c.tertiaryContainer.light,
    onTertiaryContainer: c.onTertiaryContainer.light,
    tertiaryFixed: c.tertiaryFixed.light,
    tertiaryFixedDim: c.tertiaryFixedDim.light,
    onTertiaryFixed: c.onTertiaryFixed.light,
    onTertiaryFixedVariant: c.onTertiaryFixedVariant.light,

    error: c.error.light,
    onError: c.onError.light,
    errorContainer: c.errorContainer.light,
    onErrorContainer: c.onErrorContainer.light,

    surface: c.surface.light,
    onSurface: c.onSurface.light,
    surfaceVariant: c.surfaceVariant.light,
    onSurfaceVariant: c.onSurfaceVariant.light,
    surfaceContainerLowest: c.surfaceContainerLowest.light,
    surfaceContainerLow: c.surfaceContainerLow.light,
    surfaceContainer: c.surfaceContainer.light,
    surfaceContainerHigh: c.surfaceContainerHigh.light,
    surfaceContainerHighest: c.surfaceContainerHighest.light,
    surfaceBright: c.surfaceBright.light,
    surfaceDim: c.surfaceDim.light,
    inverseSurface: c.inverseSurface.light,
    inverseOnSurface: c.inverseOnSurface.light,
    surfaceTint: c.surfaceTint.light,
    scrim: c.scrim.light,

    outline: c.outline.light,
    outlineVariant: c.outlineVariant.light,
    headerOverlay: c.headerOverlay.light,
    tabBarOverlay: c.tabBarOverlay.light,
    outlineVariantFaint: c.outlineVariantFaint.light,
    shadow: c.shadow.light,
    errorBoundarySurface: c.errorBoundarySurface.light,
    errorBoundaryTitle: c.errorBoundaryTitle.light,
    errorBoundaryBody: c.errorBoundaryBody.light,
    errorBoundaryDetail: c.errorBoundaryDetail.light,
    errorBoundaryDetailSurface: c.errorBoundaryDetailSurface.light,
    errorBoundaryAction: c.errorBoundaryAction.light,
    errorBoundaryOnAction: c.errorBoundaryOnAction.light,

    // Convenience aliases
    background: c.background.light,
    text: c.text.light,
    textSecondary: c.textSecondary.light,
    border: c.border.light,
    success: c.success.light,
    onSuccess: c.onSuccess.light,
    iconActive: c.iconActive.light,
    iconInactive: c.iconInactive.light,
  },
  typography: tokens.typography,
  spacing: tokens.spacing,
  radius: tokens.radius,
  touchTarget: tokens.touchTarget,
  isDark: false,
} as const;

// Theme type: colors are string (not literal) so darkTheme can use different hex values.
export type Theme = {
  colors: Record<keyof typeof lightTheme.colors, string>;
  typography: typeof lightTheme.typography;
  spacing: typeof lightTheme.spacing;
  radius: typeof lightTheme.radius;
  touchTarget: typeof lightTheme.touchTarget;
  isDark: boolean;
};

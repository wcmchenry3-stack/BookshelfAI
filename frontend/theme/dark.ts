import { tokens } from './tokens';
import type { Theme } from './light';

const c = tokens.colors;

export const darkTheme: Theme = {
  colors: {
    // MD3 roles
    primary: c.primary.dark,
    onPrimary: c.onPrimary.dark,
    primaryContainer: c.primaryContainer.dark,
    onPrimaryContainer: c.onPrimaryContainer.dark,
    primaryFixed: c.primaryFixed.dark,
    primaryFixedDim: c.primaryFixedDim.dark,
    onPrimaryFixed: c.onPrimaryFixed.dark,
    onPrimaryFixedVariant: c.onPrimaryFixedVariant.dark,
    inversePrimary: c.inversePrimary.dark,

    secondary: c.secondary.dark,
    onSecondary: c.onSecondary.dark,
    secondaryContainer: c.secondaryContainer.dark,
    onSecondaryContainer: c.onSecondaryContainer.dark,
    secondaryFixed: c.secondaryFixed.dark,
    secondaryFixedDim: c.secondaryFixedDim.dark,
    onSecondaryFixed: c.onSecondaryFixed.dark,
    onSecondaryFixedVariant: c.onSecondaryFixedVariant.dark,

    tertiary: c.tertiary.dark,
    onTertiary: c.onTertiary.dark,
    tertiaryContainer: c.tertiaryContainer.dark,
    onTertiaryContainer: c.onTertiaryContainer.dark,
    tertiaryFixed: c.tertiaryFixed.dark,
    tertiaryFixedDim: c.tertiaryFixedDim.dark,
    onTertiaryFixed: c.onTertiaryFixed.dark,
    onTertiaryFixedVariant: c.onTertiaryFixedVariant.dark,

    error: c.error.dark,
    onError: c.onError.dark,
    errorContainer: c.errorContainer.dark,
    onErrorContainer: c.onErrorContainer.dark,

    surface: c.surface.dark,
    onSurface: c.onSurface.dark,
    surfaceVariant: c.surfaceVariant.dark,
    onSurfaceVariant: c.onSurfaceVariant.dark,
    surfaceContainerLowest: c.surfaceContainerLowest.dark,
    surfaceContainerLow: c.surfaceContainerLow.dark,
    surfaceContainer: c.surfaceContainer.dark,
    surfaceContainerHigh: c.surfaceContainerHigh.dark,
    surfaceContainerHighest: c.surfaceContainerHighest.dark,
    surfaceBright: c.surfaceBright.dark,
    surfaceDim: c.surfaceDim.dark,
    inverseSurface: c.inverseSurface.dark,
    inverseOnSurface: c.inverseOnSurface.dark,
    surfaceTint: c.surfaceTint.dark,
    scrim: c.scrim.dark,

    outline: c.outline.dark,
    outlineVariant: c.outlineVariant.dark,

    // Convenience aliases
    background: c.background.dark,
    text: c.text.dark,
    textSecondary: c.textSecondary.dark,
    border: c.border.dark,
    success: c.success.dark,
    onSuccess: c.onSuccess.dark,
    iconActive: c.iconActive.dark,
    iconInactive: c.iconInactive.dark,
  },
  typography: tokens.typography,
  spacing: tokens.spacing,
  radius: tokens.radius,
  touchTarget: tokens.touchTarget,
  isDark: true,
};

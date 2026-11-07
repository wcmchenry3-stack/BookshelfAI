import { useTheme } from '../../hooks/useTheme';

/** Theme-derived layout/colour values the library screens and components share. */
export function useLibraryAccent() {
  const { theme } = useTheme();
  // Gold tertiary in dark mode, primary in light mode — active/CTA accent.
  const activeColor = theme.isDark ? theme.colors.tertiary : theme.colors.primary;
  const onActiveColor = theme.isDark ? theme.colors.onTertiary : theme.colors.onPrimary;
  return { activeColor, onActiveColor };
}

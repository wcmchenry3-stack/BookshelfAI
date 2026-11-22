import { useTheme } from '../../hooks/useTheme';

/** Theme-derived layout/colour values the library screens and components share. */
export function useLibraryAccent() {
  const { theme } = useTheme();
  // The single accent (primary) in both modes — active/CTA accent (#898).
  // No top padding: the header is opaque in both themes (#906).
  const activeColor = theme.colors.primary;
  const onActiveColor = theme.colors.onPrimary;
  return { activeColor, onActiveColor };
}

import { Platform } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { useTheme } from '../../hooks/useTheme';

/** Theme-derived layout/colour values the library screens and components share. */
export function useLibraryAccent() {
  const { theme } = useTheme();
  const insets = useSafeAreaInsets();
  // Transparent header only in dark mode — push content below the floating header.
  // Standard nav bar height: 44px (iOS) / 56px (Android) + safe-area top inset.
  const headerHeight = Platform.OS === 'android' ? 56 : insets.top + 44;
  const topPad = theme.isDark ? headerHeight : 0;
  // Gold tertiary in dark mode, primary in light mode — active/CTA accent.
  const activeColor = theme.isDark ? theme.colors.tertiary : theme.colors.primary;
  const onActiveColor = theme.isDark ? theme.colors.onTertiary : theme.colors.onPrimary;
  return { topPad, activeColor, onActiveColor };
}

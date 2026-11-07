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
  // The single accent (primary) in both modes — active/CTA accent (#898).
  const activeColor = theme.colors.primary;
  const onActiveColor = theme.colors.onPrimary;
  return { topPad, activeColor, onActiveColor };
}

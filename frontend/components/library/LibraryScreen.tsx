import type { ReactNode } from 'react';
import { StyleSheet, View } from 'react-native';

import { useTheme } from '../../hooks/useTheme';

/** Full-height, theme-background container shared by the My Books and Wishlist screens. */
export function LibraryScreen({ children }: { children: ReactNode }) {
  const { theme } = useTheme();
  return (
    <View style={[styles.container, { backgroundColor: theme.colors.background }]}>{children}</View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1 },
});

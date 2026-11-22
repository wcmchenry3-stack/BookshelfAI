import type { ReactNode } from 'react';
import { StyleSheet, View } from 'react-native';

import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { useTheme } from '../../hooks/useTheme';

/** Full-height, theme-background container shared by the My Books and Wishlist screens. */
export function LibraryScreen({ children, testID }: { children: ReactNode; testID?: string }) {
  const { theme } = useTheme();
  // No root header: the screen owns the top safe area (#936).
  const insets = useSafeAreaInsets();
  return (
    <View
      style={[
        styles.container,
        { backgroundColor: theme.colors.background, paddingTop: insets.top },
      ]}
      testID={testID}
    >
      {children}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1 },
});

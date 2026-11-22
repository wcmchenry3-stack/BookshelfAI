import type { ReactNode } from 'react';
import { StyleSheet, View } from 'react-native';

type Props = { children: ReactNode; testID?: string };

/**
 * Web stand-in for `GestureRoot.tsx` (#901): a full-size View, which is what
 * gesture-handler's own web `GestureHandlerRootView` renders, without importing
 * gesture-handler (and with it Reanimated and Worklets, about 200 KB gzip) into
 * the web entry bundle. It does not provide gesture-handler's root-view context,
 * so the first web feature that uses a gesture must render the real
 * `GestureHandlerRootView` here instead.
 */
export function GestureRoot({ children, testID }: Props) {
  return (
    <View style={styles.root} testID={testID}>
      {children}
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
});

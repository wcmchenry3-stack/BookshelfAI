import type { ReactNode } from 'react';
import { StyleSheet } from 'react-native';
import { GestureHandlerRootView } from 'react-native-gesture-handler';

type Props = { children: ReactNode; testID?: string };

/**
 * The one gesture-handler root at the top of the app (#901). On iOS and Android
 * react-native-gesture-handler needs it above every gesture. Content of a RN
 * `<Modal>` that uses gestures needs its own root too: native modals render in a
 * separate view hierarchy.
 *
 * Web uses `GestureRoot.web.tsx` (a plain View), so the web bundle does not pull
 * in gesture-handler, Reanimated and Worklets until a web feature imports them.
 */
export function GestureRoot({ children, testID }: Props) {
  return (
    <GestureHandlerRootView style={styles.root} testID={testID}>
      {children}
    </GestureHandlerRootView>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
});

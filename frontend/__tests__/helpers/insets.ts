import { StyleSheet, type StyleProp, type ViewStyle } from 'react-native';

/** A nonzero top safe-area inset, so tests can tell "padded by the inset" from "not padded". */
export const TEST_INSET_TOP = 47;

/** Factory for `jest.mock('react-native-safe-area-context', () => require('../helpers/insets').safeAreaModule())`. */
export function safeAreaModule() {
  return {
    useSafeAreaInsets: () => ({ top: TEST_INSET_TOP, bottom: 0, left: 0, right: 0 }),
    SafeAreaProvider: ({ children }: { children: unknown }) => children,
  };
}

type TestNode = { props: Record<string, unknown>; parent: TestNode | null };

/** True when `node` or an ancestor has `paddingTop` equal to `value` (the fixed container, not scrolled content). */
export function hasTopPadding(node: TestNode, value: number): boolean {
  for (let n: TestNode | null = node; n; n = n.parent) {
    const style = n.props?.style
      ? StyleSheet.flatten(n.props.style as StyleProp<ViewStyle>)
      : undefined;
    if (style && style.paddingTop === value) return true;
  }
  return false;
}

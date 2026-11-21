import { StyleSheet, View } from 'react-native';

import { useTheme } from '../../hooks/useTheme';
import { STATUS_COLOR_KEY } from './libraryShared';

const DOT_SIZE = 8;

/** The small coloured dot of a book's status, shared by the list row and the grid cell. */
export function StatusDot({ status, testID }: { status: string; testID?: string }) {
  const { theme } = useTheme();
  const key = status as keyof typeof STATUS_COLOR_KEY;
  const color = theme.colors[STATUS_COLOR_KEY[key] ?? 'onSurfaceVariant'];
  return <View testID={testID} style={[styles.dot, { backgroundColor: color }]} />;
}

const styles = StyleSheet.create({
  dot: { width: DOT_SIZE, height: DOT_SIZE, borderRadius: DOT_SIZE / 2 },
});

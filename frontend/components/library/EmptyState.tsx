import type { StyleProp, ViewStyle } from 'react-native';
import { StyleSheet, Text, View } from 'react-native';

import { useTheme } from '../../hooks/useTheme';

interface EmptyStateProps {
  message: string;
  style?: StyleProp<ViewStyle>;
  lineHeight?: number;
}

/** Centred "nothing here" message. */
export function EmptyState({ message, style, lineHeight }: EmptyStateProps) {
  const { theme } = useTheme();
  return (
    <View style={[styles.emptyContainer, style]}>
      <Text
        style={[
          styles.emptyText,
          { color: theme.colors.textSecondary, fontSize: theme.typography.fontSizeBase },
          lineHeight !== undefined && { lineHeight },
        ]}
      >
        {message}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  emptyContainer: { flex: 1, alignItems: 'center', justifyContent: 'center', padding: 32 },
  emptyText: { textAlign: 'center' },
});

import { ActivityIndicator, StyleSheet, Text, View } from 'react-native';

import { useTheme } from '../hooks/useTheme';

interface Props {
  message?: string;
}

export function LoadingSpinner({ message }: Props) {
  const { theme } = useTheme();

  return (
    <View style={styles.container}>
      <ActivityIndicator size="large" color={theme.colors.primary} testID="loading-spinner" />
      {message && (
        <Text
          style={[
            styles.message,
            { color: theme.colors.textSecondary, fontSize: theme.typography.fontSizeBase },
          ]}
          accessibilityLiveRegion="polite"
        >
          {message}
        </Text>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    gap: 16,
  },
  message: {
    textAlign: 'center',
  },
});

import { StyleSheet, Text, View } from 'react-native';
import { MaterialIcons } from '@expo/vector-icons';
import { useTranslation } from 'react-i18next';

import { useTheme } from '../hooks/useTheme';

/** Persistent notice shown on library screens while the device is offline. */
export function OfflineNotice() {
  const { theme } = useTheme();
  const { t } = useTranslation('common');

  return (
    <View
      testID="offline-notice"
      style={[styles.container, { backgroundColor: theme.colors.errorContainer }]}
      accessibilityRole="alert"
      accessibilityLiveRegion="polite"
    >
      <MaterialIcons name="cloud-off" size={18} color={theme.colors.onErrorContainer} />
      <Text
        style={[
          styles.text,
          { color: theme.colors.onErrorContainer, fontSize: theme.typography.fontSizeSM },
        ]}
      >
        {t('offlineEditNotice')}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
    borderRadius: 8,
    paddingHorizontal: 12,
    paddingVertical: 10,
    marginHorizontal: 16,
    marginVertical: 8,
  },
  text: { flex: 1, fontWeight: '600' },
});

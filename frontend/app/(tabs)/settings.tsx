import { View, Text, StyleSheet } from 'react-native';
import { useTranslation } from 'react-i18next';

import { useTheme } from '../../hooks/useTheme';
import { useScanJobs } from '../../hooks/useScanJobs';
import { useScanUsage } from '../../hooks/useScanUsage';
import { ThemeToggleButton } from '../../components/ThemeToggleButton';

export default function SettingsScreen() {
  const { theme } = useTheme();
  const { t } = useTranslation('settings');
  const { usageRefreshKey } = useScanJobs();
  const usage = useScanUsage(usageRefreshKey ?? 0);
  return (
    <View style={[styles.container, { backgroundColor: theme.colors.background }]}>
      <Text style={{ color: theme.colors.text, marginBottom: theme.spacing.md }}>
        {t('heading')}
      </Text>
      {usage ? (
        <View style={styles.usageRow} testID="settings-scan-usage">
          <Text style={[styles.usageLabel, { color: theme.colors.textSecondary }]}>
            {t('scanUsageLabel')}
          </Text>
          <Text style={[styles.usageValue, { color: theme.colors.text }]}>
            {t('scanUsageMonth', { used: usage.used_month, limit: usage.monthly_limit })}
          </Text>
        </View>
      ) : null}
      <ThemeToggleButton />
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, alignItems: 'center', justifyContent: 'center', paddingHorizontal: 16 },
  usageRow: { alignItems: 'center', marginBottom: 16 },
  usageLabel: { fontSize: 12 },
  usageValue: { fontSize: 14, fontWeight: '600', textAlign: 'center' },
});

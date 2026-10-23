import { View, Text, StyleSheet } from 'react-native';
import { useTranslation } from 'react-i18next';

import { useTheme } from '../../hooks/useTheme';
import { useScanJobs } from '../../hooks/useScanJobs';
import { useScanUsage } from '../../hooks/useScanUsage';
import { noMatchRemainingToday, remainingToday } from '../../components/ScanStatusPanel';
import { ThemeToggleButton } from '../../components/ThemeToggleButton';

export default function SettingsScreen() {
  const { theme } = useTheme();
  // The usage figure reuses the scan screen's "scans left today" string.
  const { t } = useTranslation(['settings', 'scan']);
  const { usageRefreshKey } = useScanJobs();
  const usage = useScanUsage(usageRefreshKey ?? 0);
  const noMatchLeft = usage ? noMatchRemainingToday(usage) : null;
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
            {t('usageToday', { ns: 'scan', count: remainingToday(usage) })}
          </Text>
          {noMatchLeft !== null ? (
            <Text
              style={[styles.usageValue, { color: theme.colors.text }]}
              testID="settings-scan-usage-no-match"
            >
              {t('usageNoMatch', { ns: 'scan', count: noMatchLeft })}
            </Text>
          ) : null}
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

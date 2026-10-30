import { useRef, useState } from 'react';
import { Alert, Platform, Pressable, View, Text, StyleSheet } from 'react-native';
import { useTranslation } from 'react-i18next';

import { useAuth } from '../../hooks/useAuth';
import { useTheme } from '../../hooks/useTheme';
import { Sentry } from '../../lib/sentry';
import { serverLogout } from '../../lib/signOut';
import { useScanJobs } from '../../hooks/useScanJobs';
import { useScanUsage } from '../../hooks/useScanUsage';
import { noMatchRemainingToday, remainingToday } from '../../components/ScanStatusPanel';
import { ThemeToggleButton } from '../../components/ThemeToggleButton';
import { useFeedback } from '../../components/FeedbackWidget/FeedbackContext';
import { openTrash } from '../../lib/trash';

export default function SettingsScreen() {
  const { theme } = useTheme();
  // The usage figure reuses the scan screen's "scans left today" string.
  const { t } = useTranslation(['settings', 'scan']);
  const { usageRefreshKey } = useScanJobs();
  const usage = useScanUsage(usageRefreshKey ?? 0);
  const noMatchLeft = usage ? noMatchRemainingToday(usage) : null;
  const { logout } = useAuth();
  const { openFeedback } = useFeedback();
  const { t: tFeedback } = useTranslation('feedback');

  const [isSigningOut, setIsSigningOut] = useState(false);
  const signingOutRef = useRef(false);

  async function performSignOut() {
    if (signingOutRef.current) return;
    signingOutRef.current = true;
    setIsSigningOut(true);
    try {
      await serverLogout();
      // AuthContext's redirect effect sends the user to the login screen.
      await logout();
    } catch {
      // Never an unhandled rejection; no values logged.
      Sentry.addBreadcrumb({ category: 'auth', message: 'sign-out failed', level: 'warning' });
    } finally {
      signingOutRef.current = false;
      setIsSigningOut(false);
    }
  }

  function confirmSignOut() {
    if (signingOutRef.current) return;
    const title = t('signOutConfirmTitle');
    const body = t('signOutConfirmBody');
    if (Platform.OS === 'web') {
      // Alert.alert with buttons is a no-op on react-native-web.
      if (window.confirm(`${title}\n\n${body}`)) void performSignOut();
      return;
    }
    Alert.alert(title, body, [
      { text: t('signOutCancel'), style: 'cancel' },
      { text: t('signOutConfirm'), style: 'destructive', onPress: () => void performSignOut() },
    ]);
  }

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
      <Pressable
        onPress={openTrash}
        style={styles.trash}
        accessibilityRole="button"
        accessibilityLabel={t('trashA11y')}
        testID="settings-trash"
      >
        <Text
          style={[
            styles.trashText,
            { color: theme.colors.text, fontSize: theme.typography.fontSizeBase },
          ]}
        >
          {t('trash')}
        </Text>
        <Text
          style={[
            styles.trashHint,
            { color: theme.colors.textSecondary, fontSize: theme.typography.fontSizeXS },
          ]}
        >
          {t('trashHint')}
        </Text>
      </Pressable>
      <Pressable
        onPress={openFeedback}
        style={styles.feedback}
        accessibilityRole="button"
        accessibilityLabel={tFeedback('title')}
        testID="settings-feedback"
      >
        <Text
          style={[
            styles.trashText,
            { color: theme.colors.text, fontSize: theme.typography.fontSizeBase },
          ]}
        >
          {tFeedback('title')}
        </Text>
      </Pressable>
      <Pressable
        onPress={confirmSignOut}
        style={[styles.signOut, isSigningOut && styles.signOutBusy]}
        accessibilityRole="button"
        disabled={isSigningOut}
        accessibilityState={{ disabled: isSigningOut, busy: isSigningOut }}
        testID="settings-sign-out"
      >
        <Text style={[styles.signOutText, { color: theme.colors.error }]}>{t('signOut')}</Text>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, alignItems: 'center', justifyContent: 'center', paddingHorizontal: 16 },
  usageRow: { alignItems: 'center', marginBottom: 16 },
  usageLabel: { fontSize: 12 },
  trash: { minHeight: 44, justifyContent: 'center', alignItems: 'center', marginTop: 16 },
  feedback: { minHeight: 44, justifyContent: 'center', alignItems: 'center', marginTop: 16 },
  trashText: { fontWeight: '600' },
  trashHint: { textAlign: 'center' },
  signOut: { minHeight: 44, justifyContent: 'center', paddingHorizontal: 16, marginTop: 16 },
  signOutBusy: { opacity: 0.5 },
  signOutText: { fontSize: 16, fontWeight: '600' },
  usageValue: { fontSize: 14, fontWeight: '600', textAlign: 'center' },
});

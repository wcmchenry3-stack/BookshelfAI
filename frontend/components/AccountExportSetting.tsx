import { useRef, useState } from 'react';
import { Alert, Platform, Pressable, StyleSheet, Text } from 'react-native';
import { useTranslation } from 'react-i18next';

import { useBanner } from '../hooks/useBanner';
import { useTheme } from '../hooks/useTheme';
import { requestAccountExport, type AccountExportResult } from '../lib/accountExport';
import { fontFor } from '../theme/typography';

const BANNER_MS = 6000;

/**
 * Settings "Export my data" row (#694). After a confirmation it asks the
 * server for a copy of the account's data; the server emails a link that
 * works for 7 days. One export a day: a second request says when the next
 * one is possible.
 */
export function AccountExportSetting() {
  const { theme } = useTheme();
  const { colors } = theme;
  const { t, i18n } = useTranslation('settings');
  const { showBanner } = useBanner();
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);

  function announce(result: AccountExportResult) {
    if (result.kind === 'queued') {
      showBanner({ message: t('exportQueued'), type: 'info', duration: BANNER_MS });
    } else if (result.kind === 'limited') {
      showBanner({
        message: t('exportLimited', { count: result.hours }),
        type: 'info',
        duration: BANNER_MS,
      });
    } else {
      const key = result.kind === 'unavailable' ? 'exportUnavailable' : 'exportFailed';
      showBanner({ message: t(key), type: 'error', duration: BANNER_MS });
    }
  }

  async function requestExport() {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    try {
      announce(await requestAccountExport(i18n.language));
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }

  function confirmExport() {
    if (busyRef.current) return;
    const title = t('exportConfirmTitle');
    const body = t('exportConfirmBody');
    if (Platform.OS === 'web') {
      // Alert.alert with buttons is a no-op on react-native-web.
      if (window.confirm(`${title}\n\n${body}`)) void requestExport();
      return;
    }
    Alert.alert(title, body, [
      { text: t('exportCancel'), style: 'cancel' },
      { text: t('exportConfirm'), onPress: () => void requestExport() },
    ]);
  }

  return (
    <Pressable
      onPress={confirmExport}
      disabled={busy}
      style={[styles.row, busy && styles.busy]}
      accessibilityRole="button"
      accessibilityHint={t('exportDataHint')}
      accessibilityState={{ disabled: busy, busy }}
      testID="settings-export-data"
    >
      <Text style={[styles.label, { color: colors.text, fontSize: theme.typography.fontSizeBase }]}>
        {t('exportData')}
      </Text>
      <Text
        style={[
          styles.hint,
          { color: colors.textSecondary, fontSize: theme.typography.fontSizeXS },
        ]}
      >
        {t('exportDataHint')}
      </Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  row: { minHeight: 44, justifyContent: 'center', alignItems: 'center', marginTop: 16 },
  busy: { opacity: 0.5 },
  label: { fontFamily: fontFor('600') },
  hint: { textAlign: 'center' },
});

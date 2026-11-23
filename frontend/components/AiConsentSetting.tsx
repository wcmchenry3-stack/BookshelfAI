import { useEffect, useRef, useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';
import { useTranslation } from 'react-i18next';

import { useAiConsent } from '../hooks/useAiConsent';
import { useBanner } from '../hooks/useBanner';
import { useTheme } from '../hooks/useTheme';
import { fontFor } from '../theme/typography';

/**
 * Settings "AI photo scanning" row (#1023): whether this account allows scan
 * photos to go to the AI provider, and a way to withdraw. Withdrawing takes
 * effect at once (the server stores it; the next scan asks again). Hidden
 * while the state cannot be read (offline): there is nothing reliable to show.
 */
export function AiConsentSetting() {
  const { theme } = useTheme();
  const { colors } = theme;
  const { t } = useTranslation('settings');
  const { showBanner } = useBanner();
  const { status, checkConsent, withdraw } = useAiConsent();
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);

  // Read it once when Settings opens and nothing has needed it yet.
  useEffect(() => {
    if (status === 'unknown') void checkConsent();
  }, [status, checkConsent]);

  async function onWithdraw() {
    if (savingRef.current) return;
    savingRef.current = true;
    setSaving(true);
    try {
      const ok = await withdraw();
      showBanner({
        message: t(ok ? 'aiConsentWithdrawn' : 'aiConsentWithdrawFailed'),
        type: ok ? 'info' : 'error',
        duration: 5000,
      });
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  }

  if (status === 'unknown') return null;
  const given = status === 'given';
  const value = t(given ? 'aiConsentOn' : 'aiConsentOff');

  return (
    <View style={styles.row} testID="settings-ai-consent">
      <View accessible accessibilityLabel={t('aiConsentA11y', { value })} style={styles.text}>
        <Text
          style={[styles.label, { color: colors.text, fontSize: theme.typography.fontSizeBase }]}
        >
          {t('aiConsent')}
        </Text>
        <Text
          style={{ color: colors.textSecondary, fontSize: theme.typography.fontSizeBase }}
          testID="settings-ai-consent-value"
        >
          {value}
        </Text>
        {!given ? (
          <Text
            style={[
              styles.hint,
              { color: colors.textSecondary, fontSize: theme.typography.fontSizeXS },
            ]}
          >
            {t('aiConsentOffHint')}
          </Text>
        ) : null}
      </View>
      {given ? (
        <Pressable
          onPress={() => void onWithdraw()}
          disabled={saving}
          style={[styles.withdraw, saving && styles.busy]}
          accessibilityRole="button"
          accessibilityHint={t('aiConsentWithdrawHint')}
          accessibilityState={{ disabled: saving, busy: saving }}
          testID="settings-ai-consent-withdraw"
        >
          <Text style={[styles.withdrawText, { color: colors.primary }]}>
            {t('aiConsentWithdraw')}
          </Text>
        </Pressable>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  row: { alignItems: 'center', marginTop: 16, paddingHorizontal: 16 },
  text: { alignItems: 'center' },
  label: { fontFamily: fontFor('600') },
  hint: { textAlign: 'center' },
  withdraw: { minHeight: 44, minWidth: 44, justifyContent: 'center', paddingHorizontal: 16 },
  busy: { opacity: 0.5 },
  withdrawText: { fontSize: 16, fontFamily: fontFor('600') },
});

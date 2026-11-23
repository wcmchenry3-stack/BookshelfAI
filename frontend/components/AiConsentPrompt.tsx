import { Linking, Modal, Pressable, ScrollView, StyleSheet, View } from 'react-native';
import { useTranslation } from 'react-i18next';

import { useModalTitleFocus } from '../hooks/useModalTitleFocus';
import { useTheme } from '../hooks/useTheme';
import { PRIVACY_POLICY_URL } from '../lib/aiConsent';
import { Sentry } from '../lib/sentry';
import { AppText } from './ui/AppText';
import { Button } from './ui/Button';
import { useFocusRing } from './ui/useFocusRing';

interface Props {
  visible: boolean;
  /** The grant call is in flight. */
  saving: boolean;
  /** The last grant call failed (shown inline, announced). */
  failed: boolean;
  onContinue: () => void;
  onNotNow: () => void;
}

/**
 * "Scanning uses AI" (#1023, Apple 5.1.2(i)): says where a scan photo goes
 * before the first one leaves the device, links the privacy policy, and
 * offers Continue (records consent on the server) or Not now. Owned by
 * AiConsentProvider; the wording is the owner's (issue #1023).
 */
export function AiConsentPrompt({ visible, saving, failed, onContinue, onNotNow }: Props) {
  const { theme } = useTheme();
  const { colors } = theme;
  const { t } = useTranslation('scan');
  const titleRef = useModalTitleFocus(visible);
  const { focusProps, focusStyle } = useFocusRing();

  function openPrivacyPolicy() {
    Linking.openURL(PRIVACY_POLICY_URL).catch(() => {
      Sentry.addBreadcrumb({
        category: 'ai-consent',
        message: 'privacy policy link failed to open',
        level: 'warning',
      });
    });
  }

  return (
    <Modal
      visible={visible}
      transparent
      animationType="fade"
      // Android back: the same as Not now (nothing is recorded).
      onRequestClose={onNotNow}
      accessibilityViewIsModal
    >
      <View style={[styles.overlay, { backgroundColor: colors.scrim }]}>
        <View
          style={[styles.card, { backgroundColor: colors.surface, borderColor: colors.outline }]}
          accessibilityViewIsModal
          testID="ai-consent-prompt"
        >
          <ScrollView contentContainerStyle={styles.content}>
            <AppText ref={titleRef} variant="sheetTitle" accessibilityRole="header">
              {t('aiConsentTitle')}
            </AppText>
            <AppText variant="body" color="onSurface">
              {t('aiConsentBody')}
            </AppText>
            <Pressable
              onPress={openPrivacyPolicy}
              accessibilityRole="link"
              accessibilityHint={t('aiConsentPrivacyHint')}
              testID="ai-consent-privacy"
              style={[styles.link, focusStyle]}
              {...focusProps}
            >
              <AppText variant="bodyStrong" color="primary" style={styles.linkText}>
                {t('aiConsentPrivacy')}
              </AppText>
            </Pressable>
            {failed ? (
              <AppText
                variant="secondary"
                color="danger"
                accessibilityRole="alert"
                accessibilityLiveRegion="polite"
                testID="ai-consent-error"
              >
                {t('aiConsentSaveFailed')}
              </AppText>
            ) : null}
          </ScrollView>
          <View style={styles.actions}>
            <Button
              label={t('aiConsentContinue')}
              onPress={onContinue}
              loading={saving}
              testID="ai-consent-continue"
            />
            <Button
              variant="text"
              label={t('aiConsentNotNow')}
              onPress={onNotNow}
              disabled={saving}
              testID="ai-consent-not-now"
            />
          </View>
        </View>
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  overlay: { flex: 1, justifyContent: 'center', padding: 24 },
  card: {
    borderRadius: 14,
    borderWidth: StyleSheet.hairlineWidth,
    alignSelf: 'center',
    width: '100%',
    maxWidth: 400,
    maxHeight: '90%',
  },
  content: { padding: 20, gap: 12 },
  link: { minHeight: 44, justifyContent: 'center', alignSelf: 'flex-start' },
  linkText: { textDecorationLine: 'underline' },
  actions: { paddingHorizontal: 20, paddingBottom: 16, gap: 4 },
});

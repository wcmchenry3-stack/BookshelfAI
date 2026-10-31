import React, { useEffect } from 'react';
import { AccessibilityInfo, Platform, StyleSheet, Text, View } from 'react-native';
import { useTranslation } from 'react-i18next';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { useTheme } from '../../hooks/useTheme';

export const THANKS_BANNER_DURATION_MS = 4000;
/** Approximate native header height; the banner sits just below it. */
const HEADER_HEIGHT = 56;

interface Props {
  visible: boolean;
  /** Keep this identity stable (useCallback), or the timer restarts on re-render. */
  onDismiss: () => void;
}

/**
 * Self-dismissing thank-you shown after feedback is sent. Removed by a timer
 * (works the same with reduce-motion) and non-interactive so it never blocks
 * the screen underneath. Android announces the polite live region; iOS ignores
 * it, so the text is also announced explicitly there.
 */
export function FeedbackThanksBanner({ visible, onDismiss }: Props) {
  const { t } = useTranslation('feedback');
  const { theme } = useTheme();
  const insets = useSafeAreaInsets();
  const message = t('submit_success');

  useEffect(() => {
    if (!visible) return;
    if (Platform.OS === 'ios') AccessibilityInfo.announceForAccessibility(message);
    const timer = setTimeout(onDismiss, THANKS_BANNER_DURATION_MS);
    return () => clearTimeout(timer);
  }, [visible, message, onDismiss]);

  if (!visible) return null;
  return (
    <View
      pointerEvents="none"
      style={[
        styles.container,
        { top: insets.top + HEADER_HEIGHT + 8, backgroundColor: theme.colors.inverseSurface },
      ]}
      accessibilityLiveRegion="polite"
      testID="feedback-thanks-banner"
    >
      <Text
        style={[
          styles.text,
          {
            color: theme.colors.inverseOnSurface,
            fontFamily: theme.typography.fontFamilyBody,
            fontSize: theme.typography.fontSizeSM,
          },
        ]}
      >
        {message}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    position: 'absolute',
    alignSelf: 'center',
    paddingHorizontal: 16,
    paddingVertical: 10,
    borderRadius: 8,
    zIndex: 100,
    // Android orders by elevation over zIndex; keep above screen content.
    elevation: 8,
    maxWidth: 320,
  },
  text: { fontWeight: '700', textAlign: 'center' },
});

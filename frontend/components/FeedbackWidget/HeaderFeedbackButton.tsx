import React from 'react';
import { Pressable, StyleSheet, Text } from 'react-native';
import { useTranslation } from 'react-i18next';
import { useTheme } from '../../hooks/useTheme';
import { useFeedback } from './FeedbackContext';

/** "?" button for the navigation header; opens the shared feedback sheet. */
export function HeaderFeedbackButton() {
  const { t } = useTranslation('feedback');
  const { theme } = useTheme();
  const { openFeedback } = useFeedback();

  return (
    <Pressable
      onPress={openFeedback}
      style={[styles.button, { backgroundColor: theme.colors.primary }]}
      accessibilityRole="button"
      accessibilityLabel={t('fab_label')}
      hitSlop={8}
      testID="header-feedback-button"
    >
      <Text
        style={[
          styles.text,
          { color: theme.colors.onPrimary, fontFamily: theme.typography.fontFamilyBody },
        ]}
      >
        ?
      </Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  // 32pt visible circle + 8pt hitSlop on each side = 48pt hit target.
  button: {
    width: 32,
    height: 32,
    borderRadius: 16,
    alignItems: 'center',
    justifyContent: 'center',
  },
  text: { fontSize: 18, fontWeight: '700', lineHeight: 22 },
});

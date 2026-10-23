import { StyleSheet, Text, View } from 'react-native';
import { useTranslation } from 'react-i18next';

import { useTheme } from '../hooks/useTheme';

/**
 * "Unverified" badge for a book the user added themselves because the catalog
 * could not find it (#616). Shown wherever such a book appears.
 */
export function UnverifiedBadge() {
  const { theme } = useTheme();
  const { t } = useTranslation('common');
  return (
    <View
      style={[styles.badge, { backgroundColor: theme.colors.tertiaryFixed }]}
      accessible
      accessibilityLabel={t('unverifiedA11y')}
      testID="unverified-badge"
    >
      <Text style={[styles.text, { color: theme.colors.onTertiaryFixed }]}>{t('unverified')}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  badge: {
    alignSelf: 'flex-start',
    paddingHorizontal: 8,
    paddingVertical: 2,
    borderRadius: 4,
    marginTop: 4,
  },
  text: { fontSize: 11, fontWeight: '600' },
});

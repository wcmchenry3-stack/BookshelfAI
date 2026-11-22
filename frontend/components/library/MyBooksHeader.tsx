import type { ReactNode } from 'react';
import { StyleSheet, View } from 'react-native';
import { useTranslation } from 'react-i18next';

import { AppText } from '../ui/AppText';
import { SCREEN_PADDING } from './libraryShared';

interface MyBooksHeaderProps {
  /** Every book in the library (not the filtered list). */
  total: number;
  /** Books being read right now; the part is left out of the subtitle when 0. */
  reading: number;
  /** Icon buttons at the end of the title row (Sort, Grid), filled in by later stories. */
  trailing?: ReactNode;
}

/** "My Books" serif title, a "10 books · 2 reading now" subtitle and a trailing action slot. */
export function MyBooksHeader({ total, reading, trailing }: MyBooksHeaderProps) {
  const { t } = useTranslation('my-books');
  const books = t('headerBooks', { count: total });
  const subtitle =
    reading > 0
      ? t('headerSubtitle', { books, reading: t('headerReading', { count: reading }) })
      : books;
  return (
    <View style={styles.row} testID="my-books-header">
      <View style={styles.text}>
        <AppText variant="screenTitle" accessibilityRole="header">
          {t('screenTitle')}
        </AppText>
        <AppText variant="secondary" color="onSurfaceVariant" testID="my-books-subtitle">
          {subtitle}
        </AppText>
      </View>
      {trailing ? <View style={styles.trailing}>{trailing}</View> : null}
    </View>
  );
}

const styles = StyleSheet.create({
  row: {
    flexDirection: 'row',
    alignItems: 'flex-end',
    justifyContent: 'space-between',
    gap: 8,
    paddingHorizontal: SCREEN_PADDING,
    paddingTop: 12,
  },
  text: { flexShrink: 1, gap: 2 },
  trailing: { flexDirection: 'row', gap: 4 },
});

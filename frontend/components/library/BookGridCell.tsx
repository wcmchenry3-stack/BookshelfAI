import { memo } from 'react';
import { Pressable, StyleSheet, View } from 'react-native';
import { useTranslation } from 'react-i18next';

import { AppText } from '../ui/AppText';
import { BookCover } from '../ui/BookCover';
import { StatusDot } from './StatusDot';
import { GRID_COVER_WIDTH, bookA11yLabel, type LibraryBook } from './libraryShared';

export interface BookGridCellProps<T extends LibraryBook> {
  item: T;
  onOpen: (item: T) => void;
}

/**
 * One cell of the My Books cover grid (#934): a decorative `md` cover, the title
 * on one line and the status dot with its name. The whole cell is one button
 * with the same label as the list row; there is no ⋯ menu (actions live in the
 * detail sheet). Memoised: `onOpen` is stable, so only a changed item re-renders.
 */
function BookGridCellBase<T extends LibraryBook>({ item, onOpen }: BookGridCellProps<T>) {
  const { t } = useTranslation('my-books');
  return (
    <Pressable
      style={styles.cell}
      onPress={() => onOpen(item)}
      accessibilityRole="button"
      accessibilityLabel={bookA11yLabel(t, item)}
      accessibilityHint={t('bookCardHint')}
      testID="book-grid-cell"
    >
      <BookCover
        title={item.book.title}
        coverUrl={item.book.cover_url}
        size="md"
        decorative
        testID="book-grid-cover"
      />
      <AppText variant="bookTitle" numberOfLines={1} style={styles.title}>
        {item.book.title}
      </AppText>
      <View style={styles.meta}>
        <StatusDot status={item.status} />
        <AppText variant="meta" color="onSurfaceVariant" numberOfLines={1} style={styles.status}>
          {t(`statusTab.${item.status}`)}
        </AppText>
      </View>
    </Pressable>
  );
}

export const BookGridCell = memo(BookGridCellBase) as typeof BookGridCellBase;

const styles = StyleSheet.create({
  cell: { width: GRID_COVER_WIDTH, minHeight: 44, gap: 6 },
  title: { fontSize: 13, lineHeight: 17 },
  meta: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  status: { flexShrink: 1, fontSize: 12, lineHeight: 16 },
});

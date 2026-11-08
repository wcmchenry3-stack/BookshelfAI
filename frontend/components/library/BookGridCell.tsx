import { memo } from 'react';
import { Pressable, StyleSheet, View } from 'react-native';
import { useTranslation } from 'react-i18next';

import { AppText } from '../ui/AppText';
import { BookCover } from '../ui/BookCover';
import { CHROME_MAX_FONT_SCALE } from '../../theme/typography';
import { StatusDot } from './StatusDot';
import {
  GRID_COVER_WIDTH,
  GRID_COVER_HEIGHT,
  bookA11yLabel,
  type LibraryBook,
} from './libraryShared';

/** Caps Dynamic Type on the one-line title and status, whose line heights are fixed. */
const GRID_MAX_FONT_SCALE = CHROME_MAX_FONT_SCALE;

export interface BookGridCellProps<T extends LibraryBook> {
  item: T;
  onOpen: (item: T) => void;
  /**
   * The cell's width (`gridCellWidth`). Below 104 (a phone narrower than 376dp, which
   * cannot fit three full covers) the cover is scaled down to fit. A primitive, so memo holds.
   */
  cellWidth?: number;
}

/**
 * One cell of the My Books cover grid (#934): a decorative `md` cover, the title
 * on one line and the status dot with its name. The whole cell is one button
 * with the same label as the list row; there is no ⋯ menu (actions live in the
 * detail sheet). Memoised: `onOpen` is stable, so only a changed item re-renders.
 */
function BookGridCellBase<T extends LibraryBook>({
  item,
  onOpen,
  cellWidth = GRID_COVER_WIDTH,
}: BookGridCellProps<T>) {
  const { t } = useTranslation('my-books');
  return (
    <Pressable
      style={[styles.cell, { width: cellWidth }]}
      onPress={() => onOpen(item)}
      accessibilityRole="button"
      accessibilityLabel={bookA11yLabel(t, item)}
      accessibilityHint={t('bookCardHint')}
      testID="book-grid-cell"
    >
      {cellWidth < GRID_COVER_WIDTH ? (
        // BookCover has fixed sizes: shrink the md cover with a transform inside a box of the scaled size.
        <View
          testID="book-grid-cover-scaled"
          style={{ width: cellWidth, height: Math.round(cellWidth * 1.5), overflow: 'hidden' }}
        >
          <View style={[styles.scaled, { transform: [{ scale: cellWidth / GRID_COVER_WIDTH }] }]}>
            <BookCover
              title={item.book.title}
              coverUrl={item.book.cover_url}
              size="md"
              decorative
              testID="book-grid-cover"
            />
          </View>
        </View>
      ) : (
        <BookCover
          title={item.book.title}
          coverUrl={item.book.cover_url}
          size="md"
          decorative
          testID="book-grid-cover"
        />
      )}
      <AppText
        variant="bookTitle"
        numberOfLines={1}
        maxFontSizeMultiplier={GRID_MAX_FONT_SCALE}
        style={styles.title}
      >
        {item.book.title}
      </AppText>
      <View style={styles.meta}>
        <StatusDot status={item.status} />
        <AppText
          variant="meta"
          color="onSurfaceVariant"
          numberOfLines={1}
          maxFontSizeMultiplier={GRID_MAX_FONT_SCALE}
          style={styles.status}
        >
          {t(`statusTab.${item.status}`)}
        </AppText>
      </View>
    </Pressable>
  );
}

export const BookGridCell = memo(BookGridCellBase) as typeof BookGridCellBase;

const styles = StyleSheet.create({
  cell: { minHeight: 44, gap: 6 },
  scaled: {
    width: GRID_COVER_WIDTH,
    height: GRID_COVER_HEIGHT,
    transformOrigin: 'top left',
  },
  title: { fontSize: 13, lineHeight: 17 },
  meta: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  status: { flexShrink: 1, fontSize: 12, lineHeight: 16 },
});

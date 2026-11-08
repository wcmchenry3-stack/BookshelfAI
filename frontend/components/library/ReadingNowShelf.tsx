import { memo } from 'react';
import { Pressable, ScrollView, StyleSheet, View } from 'react-native';
import { useTranslation } from 'react-i18next';

import { AppText } from '../ui/AppText';
import { BookCover, COVER_SIZES } from '../ui/BookCover';
import { SectionLabel } from '../ui/SectionLabel';
import { SCREEN_PADDING, type LibraryBook } from './libraryShared';

interface ReadingNowShelfProps<T extends LibraryBook> {
  /** The books being read; the caller only mounts the shelf when there is at least one. */
  books: T[];
  onOpen: (item: T) => void;
}

interface ShelfItemProps<T extends LibraryBook> {
  item: T;
  onOpen: (item: T) => void;
}

/** One shelf book: an `md` cover with the title and author under it. The whole item is the button. */
const ShelfItem = memo(function ShelfItem<T extends LibraryBook>({
  item,
  onOpen,
}: ShelfItemProps<T>) {
  const { t } = useTranslation('my-books');
  const { title, author, cover_url } = item.book;
  return (
    <View role="listitem">
      <Pressable
        onPress={() => onOpen(item)}
        accessibilityRole="button"
        accessibilityLabel={author ? t('shelfItemA11y', { title, author }) : title}
        accessibilityHint={t('bookCardHint')}
        testID={`reading-shelf-item-${item.id}`}
        style={({ pressed }) => [styles.item, { opacity: pressed ? 0.7 : 1 }]}
      >
        <BookCover title={title} coverUrl={cover_url} size="md" decorative />
        <View style={styles.text} pointerEvents="none">
          <AppText variant="bodyStrong" numberOfLines={1}>
            {title}
          </AppText>
          <AppText variant="meta" color="onSurfaceVariant" numberOfLines={1}>
            {author}
          </AppText>
        </View>
      </Pressable>
    </View>
  );
}) as <T extends LibraryBook>(props: ShelfItemProps<T>) => React.ReactElement;

/**
 * "Reading now" (#933): a horizontal shelf of the books being read, announced as a
 * list ("Reading now, 2 books"). Tapping a book opens its detail sheet.
 */
export function ReadingNowShelf<T extends LibraryBook>({ books, onOpen }: ReadingNowShelfProps<T>) {
  const { t } = useTranslation('my-books');
  return (
    <View style={styles.section} testID="reading-shelf">
      <SectionLabel style={styles.label}>{t('section.readingNow')}</SectionLabel>
      <ScrollView
        horizontal
        showsHorizontalScrollIndicator={false}
        role="list"
        accessibilityLabel={t('shelfA11y', { count: books.length })}
        contentContainerStyle={styles.content}
        style={styles.scroll}
      >
        {books.map((item) => (
          <ShelfItem key={item.id} item={item} onOpen={onOpen} />
        ))}
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  section: { paddingTop: 24, gap: 12 },
  label: { paddingHorizontal: SCREEN_PADDING },
  scroll: { flexGrow: 0 },
  // Room below for the cover shadow; the items are 16px apart.
  content: { paddingHorizontal: SCREEN_PADDING, paddingBottom: 6, gap: 16 },
  item: { width: COVER_SIZES.md.width, gap: 8 },
  text: { gap: 2 },
});

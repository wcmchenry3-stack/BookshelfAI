import { memo, type ReactNode } from 'react';
import { Pressable, StyleSheet, View } from 'react-native';
import { useTranslation } from 'react-i18next';

import { AppText } from '../ui/AppText';
import { BookCover } from '../ui/BookCover';
import { UnverifiedBadge } from '../UnverifiedBadge';
import { useTheme } from '../../hooks/useTheme';
import { ROW_DIVIDER_INSET, STATUS_COLOR_KEY, type LibraryBook } from './libraryShared';

export interface LibraryRowProps {
  item: LibraryBook;
  /**
   * When set, the whole row opens something. The button is a sibling overlay,
   * never a wrapper of the trailing control (#907).
   */
  onPress?: () => void;
  /** The control at the end of the row (a 44x44 target), e.g. an IconButton or chip. */
  trailing?: ReactNode;
  /** Spoken label of the body button (translated by the caller). */
  accessibilityLabel?: string;
  accessibilityHint?: string;
  /** Show the status dot and name in the meta line. Defaults to true. */
  showStatus?: boolean;
  testID?: string;
}

/** The row's minimum height; it grows with Dynamic Type. */
export const ROW_MIN_HEIGHT = 96;

/**
 * One row of the library list: a 48x72 cover, serif title, author and a meta
 * line (status dot, status, year), with an optional trailing control. Rows sit
 * edge to edge; `LibraryRowSeparator` draws the hairline between them.
 */
function LibraryRowBase({
  item,
  onPress,
  trailing,
  accessibilityLabel,
  accessibilityHint,
  showStatus = true,
  testID = 'book-row',
}: LibraryRowProps) {
  const { theme } = useTheme();
  const { t } = useTranslation('my-books');

  const year = item.edition?.publish_year ?? null;
  const statusKey = item.status as keyof typeof STATUS_COLOR_KEY;
  const dotColor = theme.colors[STATUS_COLOR_KEY[statusKey] ?? 'onSurfaceVariant'];
  const status = t(`statusTab.${item.status}`);
  const meta = showStatus
    ? year
      ? t('rowMeta', { status, year })
      : status
    : year
      ? String(year)
      : null;

  // When the row is tappable the cover and text are inert (`pointerEvents="none"`)
  // so a tap falls through to the sibling overlay button, whose label already
  // carries title, status and author; the inert block is hidden from assistive
  // tech so the same content is not announced twice.
  const inertProps = onPress
    ? ({
        pointerEvents: 'none',
        accessibilityElementsHidden: true,
        importantForAccessibility: 'no-hide-descendants',
        'aria-hidden': true,
      } as const)
    : ({ pointerEvents: 'auto' } as const);

  return (
    <View style={styles.row} testID={testID}>
      {onPress ? (
        <Pressable
          style={styles.overlay}
          onPress={onPress}
          accessibilityRole="button"
          accessibilityLabel={accessibilityLabel}
          accessibilityHint={accessibilityHint}
          testID="book-row-body"
        />
      ) : null}
      <View style={styles.content} pointerEvents="box-none">
        <View testID="book-row-cover-wrap" {...inertProps}>
          <BookCover
            title={item.book.title}
            coverUrl={item.book.cover_url}
            size="xs"
            decorative
            testID="book-row-cover"
          />
        </View>
        <View style={styles.textBlock} testID="book-row-text" {...inertProps}>
          <AppText variant="bookTitle" numberOfLines={1}>
            {item.book.title}
          </AppText>
          <AppText variant="secondary" color="onSurfaceVariant" numberOfLines={1}>
            {item.book.author}
          </AppText>
          {meta ? (
            <View style={styles.metaLine}>
              {showStatus ? (
                <View testID="book-row-dot" style={[styles.dot, { backgroundColor: dotColor }]} />
              ) : null}
              <AppText variant="meta" color="onSurfaceVariant" style={styles.metaText}>
                {meta}
              </AppText>
            </View>
          ) : null}
          {item.unverified ? <UnverifiedBadge /> : null}
        </View>
        {trailing ? (
          <View style={styles.trailing} testID="book-row-trailing">
            {trailing}
          </View>
        ) : null}
      </View>
    </View>
  );
}

export const LibraryRow = memo(LibraryRowBase);

/** The hairline between two rows, inset to start at the text column. */
export function LibraryRowSeparator() {
  const { theme } = useTheme();
  return (
    <View
      testID="book-row-divider"
      style={[styles.divider, { backgroundColor: theme.colors.outline }]}
    />
  );
}

const DOT_SIZE = 8;

const styles = StyleSheet.create({
  row: { minHeight: ROW_MIN_HEIGHT, justifyContent: 'center' },
  overlay: { position: 'absolute', top: 0, right: 0, bottom: 0, left: 0 },
  content: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 14,
    paddingHorizontal: 16,
    paddingVertical: 12,
  },
  textBlock: { flex: 1, minWidth: 0, gap: 3 },
  metaLine: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  dot: { width: DOT_SIZE, height: DOT_SIZE, borderRadius: DOT_SIZE / 2 },
  metaText: { flexShrink: 1 },
  trailing: {},
  divider: { height: StyleSheet.hairlineWidth, marginLeft: ROW_DIVIDER_INSET },
});

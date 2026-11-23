import { memo, type ReactNode } from 'react';
import { Platform, Pressable, StyleSheet, View, type AccessibilityActionEvent } from 'react-native';
import { useTranslation } from 'react-i18next';

import { AppText } from '../ui/AppText';
import { BookCover } from '../ui/BookCover';
import { UnverifiedBadge } from '../UnverifiedBadge';
import { useTheme } from '../../hooks/useTheme';
import { fontFor } from '../../theme/typography';
import { StatusDot } from './StatusDot';
import { SwipeToRemove } from './SwipeToRemove';
import type { SwipeRowGroup } from './swipeToRemoveShared';
import { ROW_DIVIDER_INSET, type LibraryBook } from './libraryShared';

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
  /**
   * Replaces the meta line (status dot, status, year) with this text, for a
   * list whose second line is not the book's status, e.g. Trash's days left.
   */
  metaText?: string;
  /** `danger` draws `metaText` in the danger colour, semibold. */
  metaTone?: 'default' | 'danger';
  /** Show the status dot and name in the meta line. Defaults to true. */
  showStatus?: boolean;
  /**
   * Wishlist (#935): removes the book. On iOS and Android the row can then be
   * swiped to reveal Remove (`SwipeToRemove`), and its body button gets a
   * `remove` screen-reader action. Web has neither: it removes through the
   * detail sheet. Needs `onPress`, `removeActionLabel` and `swipeRemoveLabel`.
   */
  onSwipeRemove?: () => Promise<boolean>;
  /** Visible text of the swipe panel and name of the `remove` action, e.g. "Remove". */
  removeActionLabel?: string;
  /** Spoken label of the swipe panel's button, e.g. "Remove Dune from wishlist". */
  swipeRemoveLabel?: string;
  /** Turns the swipe off (offline, or a change to the book in flight). */
  swipeDisabled?: boolean;
  /** The list's one-open-row group. */
  swipeGroup?: SwipeRowGroup;
  testID?: string;
}

const REMOVE_ACTION = 'remove';

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
  metaText,
  metaTone = 'default',
  showStatus = true,
  onSwipeRemove,
  removeActionLabel = '',
  swipeRemoveLabel = '',
  swipeDisabled = false,
  swipeGroup,
  testID = 'book-row',
}: LibraryRowProps) {
  const { t } = useTranslation('my-books');

  const year = item.edition?.publish_year ?? null;
  const status = t(`rowStatus.${item.status}`);
  const showDot = showStatus && metaText === undefined;
  const meta =
    metaText !== undefined
      ? metaText
      : showStatus
        ? year
          ? t('rowMeta', { status, year })
          : status
        : year
          ? String(year)
          : null;

  // A row with no body button reads as one element when it is given a label
  // (the trailing control stays separate): title, author and meta in one go.
  // The caller's label must then carry everything in the block, e.g. the
  // unverified badge. Native only: react-native-web does not pass `accessible`
  // to the DOM, so on web the child text is still read piece by piece.
  const readAsOne = !onPress && accessibilityLabel !== undefined;

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

  // Screen readers cannot swipe: the body button carries the removal as a
  // custom action instead (VoiceOver Actions rotor, TalkBack actions menu).
  // Activating the row still opens it. Native only, like the swipe.
  const removeAction = onSwipeRemove && Platform.OS !== 'web';
  const onAccessibilityAction = (event: AccessibilityActionEvent) => {
    if (event.nativeEvent.actionName === REMOVE_ACTION) void onSwipeRemove?.();
  };

  const row = (
    <View style={styles.row} testID={testID}>
      {onPress ? (
        <Pressable
          style={styles.overlay}
          onPress={onPress}
          accessibilityRole="button"
          accessibilityLabel={accessibilityLabel}
          accessibilityHint={accessibilityHint}
          accessibilityActions={
            removeAction ? [{ name: REMOVE_ACTION, label: removeActionLabel }] : undefined
          }
          onAccessibilityAction={removeAction ? onAccessibilityAction : undefined}
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
        <View
          style={styles.textBlock}
          testID="book-row-text"
          accessible={readAsOne || undefined}
          accessibilityLabel={readAsOne ? accessibilityLabel : undefined}
          {...inertProps}
        >
          <AppText variant="bookTitle" numberOfLines={1}>
            {item.book.title}
          </AppText>
          <AppText variant="secondary" color="onSurfaceVariant" numberOfLines={1}>
            {item.book.author}
          </AppText>
          {meta ? (
            <View style={styles.metaLine}>
              {showDot ? <StatusDot status={item.status} testID="book-row-dot" /> : null}
              <AppText
                variant="meta"
                color={metaTone === 'danger' ? 'danger' : 'onSurfaceVariant'}
                style={[styles.metaText, metaTone === 'danger' && styles.metaDanger]}
              >
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

  if (!onSwipeRemove) return row;
  return (
    <SwipeToRemove
      onRemove={onSwipeRemove}
      label={swipeRemoveLabel}
      actionText={removeActionLabel}
      disabled={swipeDisabled}
      group={swipeGroup}
    >
      {row}
    </SwipeToRemove>
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
  metaText: { flexShrink: 1 },
  metaDanger: { fontFamily: fontFor('600') },
  trailing: {},
  divider: { height: StyleSheet.hairlineWidth, marginLeft: ROW_DIVIDER_INSET },
});

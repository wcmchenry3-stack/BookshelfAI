import type { ReactNode } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { BookCover } from '../ui/BookCover';
import { UnverifiedBadge } from '../UnverifiedBadge';
import { useTheme } from '../../hooks/useTheme';
import { fontFor } from '../../theme/typography';
import { useLibraryAccent } from './useLibraryAccent';
import type { LibraryBook } from './libraryShared';

interface BookRowProps {
  item: LibraryBook;
  /** The small uppercase label above the title (e.g. the status). */
  categoryLabel: string;
  /**
   * When set the whole card is tappable (My Books opens its detail sheet). The
   * button is a sibling overlay, not a wrapper of the actions.
   */
  onPress?: () => void;
  accessibilityLabel?: string;
  accessibilityHint?: string;
  /** The action buttons, rendered under the title block. */
  children: ReactNode;
}

/** One bento card in the library list: cover, title block and actions. */
export function BookRow({
  item,
  categoryLabel,
  onPress,
  accessibilityLabel,
  accessibilityHint,
  children,
}: BookRowProps) {
  const { theme } = useTheme();
  const { activeColor } = useLibraryAccent();

  const cardStyle = [
    styles.card,
    { backgroundColor: theme.colors.surfaceContainerLow, borderLeftColor: activeColor },
  ];

  // `sm` (52x78) is the closest BookCover size to the old 72x108 frame. The row's
  // text announces the title (the card label when tappable), so the cover is decorative.
  const cover = (
    <BookCover
      title={item.book.title}
      coverUrl={item.book.cover_url}
      size="sm"
      decorative
      testID="book-row-cover"
    />
  );

  // When the card is tappable the text/cover are inert (`pointerEvents="none"`)
  // so a tap falls through to the sibling overlay button; the action buttons
  // are siblings of that overlay, never nested inside it (#907).
  // Its label already carries title/status, so the inert block is also hidden
  // from assistive tech to avoid announcing the same content twice.
  const inertProps = onPress
    ? ({
        pointerEvents: 'none',
        accessibilityElementsHidden: true,
        importantForAccessibility: 'no-hide-descendants',
        'aria-hidden': true,
      } as const)
    : ({ pointerEvents: 'auto' } as const);

  return (
    <View style={cardStyle}>
      {onPress ? (
        <Pressable
          style={styles.cardPressable}
          onPress={onPress}
          accessibilityRole="button"
          accessibilityLabel={accessibilityLabel}
          accessibilityHint={accessibilityHint}
          testID="book-row-body"
        />
      ) : null}
      <View style={styles.cardContent} pointerEvents="box-none">
        <View testID="book-row-cover-wrap" style={styles.coverWrap} {...inertProps}>
          {cover}
        </View>
        <View style={styles.content} pointerEvents="box-none">
          <View style={styles.textBlock} testID="book-row-text" {...inertProps}>
            <Text
              style={[
                styles.categoryLabel,
                { color: activeColor, fontSize: theme.typography.fontSizeXS },
              ]}
            >
              {categoryLabel}
            </Text>

            <Text
              style={[
                styles.bookTitle,
                { color: theme.colors.text, fontSize: theme.typography.fontSizeBase },
              ]}
              numberOfLines={2}
            >
              {item.book.title}
            </Text>

            <Text
              style={[
                styles.bookAuthor,
                { color: theme.colors.textSecondary, fontSize: theme.typography.fontSizeSM },
              ]}
              numberOfLines={1}
            >
              {item.book.author}
            </Text>
            {item.unverified ? <UnverifiedBadge /> : null}

            {item.edition?.publish_year ? (
              <Text
                style={[
                  styles.bookYear,
                  { color: theme.colors.textSecondary, fontSize: theme.typography.fontSizeXS },
                ]}
              >
                {item.edition.publish_year}
              </Text>
            ) : null}
          </View>

          <View style={styles.actions}>{children}</View>
        </View>
      </View>
    </View>
  );
}

interface BookRowActionProps {
  label: string;
  accessibilityLabel: string;
  onPress: () => void;
  disabled: boolean;
  /** `primary` is the accent-coloured CTA; `secondary` the muted remove button. */
  variant: 'primary' | 'secondary';
}

/** A small action button on a BookRow. */
export function BookRowAction({
  label,
  accessibilityLabel,
  onPress,
  disabled,
  variant,
}: BookRowActionProps) {
  const { theme } = useTheme();
  const { activeColor, onActiveColor } = useLibraryAccent();
  const primary = variant === 'primary';
  return (
    <Pressable
      style={[
        styles.actionButton,
        { backgroundColor: primary ? activeColor : theme.colors.secondaryContainer },
        disabled && styles.actionDisabled,
      ]}
      onPress={onPress}
      disabled={disabled}
      hitSlop={ACTION_HIT_SLOP}
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel}
      accessibilityState={{ disabled }}
    >
      <Text
        style={[
          styles.actionText,
          {
            fontSize: theme.typography.fontSizeXS,
            color: primary ? onActiveColor : theme.colors.onSecondaryContainer,
          },
        ]}
      >
        {label}
      </Text>
    </Pressable>
  );
}

/** 32px visual button + 6px slop top/bottom = 44px touch target. */
const ACTION_HIT_SLOP = { top: 6, bottom: 6, left: 4, right: 4 };

const styles = StyleSheet.create({
  card: {
    borderRadius: 12,
    overflow: 'hidden',
    borderLeftWidth: 4,
  },
  cardPressable: { position: 'absolute', top: 0, right: 0, bottom: 0, left: 0 },
  cardContent: { flexDirection: 'row', alignItems: 'flex-start' },
  coverWrap: { marginLeft: 12, marginVertical: 12 },
  textBlock: { gap: 4 },
  content: { flex: 1, padding: 12, gap: 4 },
  categoryLabel: {
    textTransform: 'uppercase',
    letterSpacing: 1,
    fontFamily: fontFor('700'),
    marginBottom: 2,
  },
  bookTitle: { fontFamily: fontFor('600'), lineHeight: 22 },
  bookAuthor: { lineHeight: 18 },
  bookYear: { lineHeight: 16 },
  actions: { flexDirection: 'row', gap: 8, marginTop: 8, flexWrap: 'wrap' },
  actionButton: {
    paddingHorizontal: 12,
    paddingVertical: 6,
    borderRadius: 6,
    minHeight: 32,
    minWidth: 44,
    justifyContent: 'center',
  },
  actionText: { fontFamily: fontFor('600') },
  actionDisabled: { opacity: 0.5 },
});

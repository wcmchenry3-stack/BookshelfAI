import type { ReactNode } from 'react';
import { Image, Pressable, StyleSheet, Text, View } from 'react-native';
import { useTranslation } from 'react-i18next';

import { UnverifiedBadge } from '../UnverifiedBadge';
import { useTheme } from '../../hooks/useTheme';
import { useLibraryAccent } from './useLibraryAccent';
import type { LibraryBook } from './libraryShared';

interface BookRowProps {
  item: LibraryBook;
  /** The small uppercase label above the title (e.g. the status). */
  categoryLabel: string;
  /** When set the whole card is a button (a sibling overlay, not a wrapper of the actions) (My Books opens its detail sheet). */
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
  const { t } = useTranslation('common');
  const { activeColor } = useLibraryAccent();

  const cardStyle = [
    styles.card,
    { backgroundColor: theme.colors.surfaceContainerLow, borderLeftColor: activeColor },
  ];

  const cover = item.book.cover_url ? (
    <Image
      source={{ uri: item.book.cover_url }}
      style={styles.cover}
      resizeMode="cover"
      accessibilityLabel={t('coverAlt', { title: item.book.title })}
      testID="book-row-cover"
    />
  ) : (
    <View
      style={[styles.cover, styles.coverPlaceholder, { backgroundColor: theme.colors.border }]}
      accessibilityLabel={t('noCoverAvailable')}
      testID="book-row-cover"
    />
  );

  // When the card is tappable the text/cover are inert (`pointerEvents="none"`)
  // so a tap falls through to the sibling overlay button; the action buttons
  // are siblings of that overlay, never nested inside it (#907).
  const inert = onPress ? ('none' as const) : ('auto' as const);

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
        <View pointerEvents={inert}>{cover}</View>
        <View style={styles.content} pointerEvents="box-none">
          <View style={styles.textBlock} pointerEvents={inert}>
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

/** Fixed 2:3 cover frame; the card height follows it, never the reverse (#911). */
export const COVER_WIDTH = 72;
export const COVER_HEIGHT = 108;

const styles = StyleSheet.create({
  card: {
    borderRadius: 12,
    overflow: 'hidden',
    borderLeftWidth: 4,
  },
  cardPressable: { position: 'absolute', top: 0, right: 0, bottom: 0, left: 0 },
  cardContent: { flexDirection: 'row', alignItems: 'flex-start' },
  cover: {
    width: COVER_WIDTH,
    height: COVER_HEIGHT,
    marginLeft: 12,
    marginVertical: 12,
    borderRadius: 6,
  },
  textBlock: { gap: 4 },
  coverPlaceholder: { opacity: 0.4 },
  content: { flex: 1, padding: 12, gap: 4 },
  categoryLabel: {
    textTransform: 'uppercase',
    letterSpacing: 1,
    fontWeight: '700',
    marginBottom: 2,
  },
  bookTitle: { fontWeight: '600', lineHeight: 22 },
  bookAuthor: { lineHeight: 18 },
  bookYear: { lineHeight: 16 },
  actions: { flexDirection: 'row', gap: 8, marginTop: 8, flexWrap: 'wrap' },
  actionButton: {
    paddingHorizontal: 12,
    paddingVertical: 6,
    borderRadius: 6,
    minHeight: 32,
    justifyContent: 'center',
  },
  actionText: { fontWeight: '600' },
  actionDisabled: { opacity: 0.5 },
});

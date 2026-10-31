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
  /** When set the whole card is a button (My Books opens its detail sheet). */
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

  const body = (
    <>
      {/* Cover */}
      {item.book.cover_url ? (
        <Image
          source={{ uri: item.book.cover_url }}
          style={styles.cover}
          resizeMode="cover"
          accessibilityLabel={t('coverAlt', { title: item.book.title })}
        />
      ) : (
        <View
          style={[styles.cover, styles.coverPlaceholder, { backgroundColor: theme.colors.border }]}
          accessibilityLabel={t('noCoverAvailable')}
        />
      )}

      {/* Content */}
      <View style={styles.content}>
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

        <View style={styles.actions}>{children}</View>
      </View>
    </>
  );

  if (!onPress) return <View style={cardStyle}>{body}</View>;
  return (
    <Pressable
      style={cardStyle}
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel}
      accessibilityHint={accessibilityHint}
    >
      {body}
    </Pressable>
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

const styles = StyleSheet.create({
  card: {
    flexDirection: 'row',
    borderRadius: 12,
    overflow: 'hidden',
    minHeight: 120,
    borderLeftWidth: 4,
  },
  cover: { width: 110, alignSelf: 'stretch' },
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

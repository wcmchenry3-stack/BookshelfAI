import { Image, Modal, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { useTranslation } from 'react-i18next';

import { UnverifiedBadge } from '../UnverifiedBadge';
import { useTheme } from '../../hooks/useTheme';
import { useLibraryAccent } from './useLibraryAccent';
import { NEXT_STATUS, type LibraryBook } from './libraryShared';

export interface DetailBook extends LibraryBook {
  book: LibraryBook['book'] & { description: string | null };
  edition: { publish_year: number | null; page_count: number | null } | null;
}

interface BookDetailSheetProps<T extends DetailBook> {
  selected: T | null;
  actionsDisabled: boolean;
  onClose: () => void;
  onAdvance: (item: T) => void;
  onRemove: (item: T) => void;
}

/** My Books detail sheet — full book info, advance status, remove. */
export function BookDetailSheet<T extends DetailBook>({
  selected,
  actionsDisabled,
  onClose,
  onAdvance,
  onRemove,
}: BookDetailSheetProps<T>) {
  const { theme } = useTheme();
  const { t } = useTranslation('my-books');
  const { activeColor, onActiveColor } = useLibraryAccent();
  const nextStatus = selected ? NEXT_STATUS[selected.status] : undefined;

  return (
    <Modal
      visible={!!selected}
      animationType="slide"
      presentationStyle="pageSheet"
      onRequestClose={onClose}
      accessibilityViewIsModal
    >
      {selected && (
        <View style={[styles.sheet, { backgroundColor: theme.colors.surface }]}>
          <View style={[styles.sheetHeader, { borderBottomColor: theme.colors.border }]}>
            <Text
              style={[
                styles.sheetTitle,
                { color: theme.colors.text, fontSize: theme.typography.fontSizeLG },
              ]}
              accessibilityRole="header"
              numberOfLines={2}
            >
              {selected.book.title}
            </Text>
            <Pressable
              onPress={onClose}
              accessibilityRole="button"
              accessibilityLabel={t('closeDetail')}
              style={styles.closeButton}
              hitSlop={8}
            >
              <Text style={{ color: activeColor, fontSize: theme.typography.fontSizeBase }}>
                {t('close', { ns: 'common' })}
              </Text>
            </Pressable>
          </View>

          <ScrollView contentContainerStyle={styles.sheetContent}>
            {selected.book.cover_url && (
              <Image
                source={{ uri: selected.book.cover_url }}
                style={styles.sheetCover}
                accessibilityLabel={t('coverAlt', { ns: 'common', title: selected.book.title })}
              />
            )}
            <Text
              style={[
                styles.sheetAuthor,
                { color: theme.colors.textSecondary, fontSize: theme.typography.fontSizeBase },
              ]}
            >
              {selected.book.author}
            </Text>
            {selected.unverified ? <UnverifiedBadge /> : null}
            {selected.book.description && (
              <Text
                style={[
                  styles.sheetDescription,
                  { color: theme.colors.text, fontSize: theme.typography.fontSizeSM },
                ]}
              >
                {selected.book.description}
              </Text>
            )}
            {selected.edition?.page_count && (
              <Text
                style={[
                  { color: theme.colors.textSecondary, fontSize: theme.typography.fontSizeSM },
                ]}
              >
                {t('pages', { count: selected.edition.page_count })}
              </Text>
            )}

            {nextStatus && (
              <Pressable
                style={[
                  styles.sheetButton,
                  { backgroundColor: activeColor },
                  actionsDisabled && styles.actionDisabled,
                ]}
                onPress={() => onAdvance(selected)}
                disabled={actionsDisabled}
                accessibilityRole="button"
                accessibilityLabel={t('markAsA11y', { status: t(`status.${nextStatus}`) })}
                accessibilityState={{ disabled: actionsDisabled }}
              >
                <Text
                  style={[
                    styles.sheetButtonText,
                    { fontSize: theme.typography.fontSizeBase, color: onActiveColor },
                  ]}
                >
                  {t('markAs', { status: t(`status.${nextStatus}`) })}
                </Text>
              </Pressable>
            )}

            <Pressable
              style={[
                styles.sheetButton,
                { backgroundColor: theme.colors.surfaceContainerHigh },
                actionsDisabled && styles.actionDisabled,
              ]}
              onPress={() => onRemove(selected)}
              disabled={actionsDisabled}
              accessibilityRole="button"
              accessibilityLabel={t('removeBookA11y', { title: selected.book.title })}
              accessibilityState={{ disabled: actionsDisabled }}
            >
              <Text
                style={[
                  styles.sheetButtonText,
                  {
                    color: theme.colors.onSurfaceVariant,
                    fontSize: theme.typography.fontSizeBase,
                  },
                ]}
              >
                {t('removeFromList')}
              </Text>
            </Pressable>
          </ScrollView>
        </View>
      )}
    </Modal>
  );
}

const styles = StyleSheet.create({
  sheet: { flex: 1 },
  sheetHeader: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: 16,
    paddingVertical: 16,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  sheetTitle: { fontWeight: '700', flex: 1, marginRight: 8 },
  closeButton: { minWidth: 44, minHeight: 44, justifyContent: 'center', alignItems: 'flex-end' },
  sheetContent: { padding: 16, gap: 12, alignItems: 'center' },
  sheetCover: { width: 120, height: 180, borderRadius: 4 },
  sheetAuthor: { alignSelf: 'flex-start' },
  sheetDescription: { lineHeight: 20 },
  sheetButton: {
    width: '100%',
    paddingVertical: 14,
    borderRadius: 8,
    alignItems: 'center',
    minHeight: 44,
    justifyContent: 'center',
  },
  sheetButtonText: { fontWeight: '600' },
  actionDisabled: { opacity: 0.5 },
});

import { Image, ScrollView, StyleSheet, Text } from 'react-native';
import { useTranslation } from 'react-i18next';

import SheetModal from '../SheetModal';
import { Button } from '../ui/Button';
import { UnverifiedBadge } from '../UnverifiedBadge';
import { useTheme } from '../../hooks/useTheme';
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
  // Reads the `my-books` namespace (My Books only).
  const { t } = useTranslation('my-books');
  const nextStatus = selected ? NEXT_STATUS[selected.status] : undefined;

  return (
    <SheetModal
      visible={!!selected}
      onDismiss={onClose}
      title={selected?.book.title ?? ''}
      subtitle={selected?.book.author}
      closeLabel={t('close', { ns: 'common' })}
      closeA11yLabel={t('closeDetail')}
      footer={
        selected ? (
          <>
            {nextStatus && (
              <Button
                label={t('markAs', { status: t(`status.${nextStatus}`) })}
                accessibilityLabel={t('markAsA11y', { status: t(`status.${nextStatus}`) })}
                onPress={() => onAdvance(selected)}
                disabled={actionsDisabled}
              />
            )}
            <Button
              variant="secondary"
              label={t('removeFromList')}
              accessibilityLabel={t('removeBookA11y', { title: selected.book.title })}
              onPress={() => onRemove(selected)}
              disabled={actionsDisabled}
            />
          </>
        ) : null
      }
    >
      {selected && (
        <ScrollView contentContainerStyle={styles.sheetContent}>
          {selected.book.cover_url && (
            <Image
              source={{ uri: selected.book.cover_url }}
              style={styles.sheetCover}
              accessibilityLabel={t('coverAlt', { ns: 'common', title: selected.book.title })}
            />
          )}
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
              style={[{ color: theme.colors.textSecondary, fontSize: theme.typography.fontSizeSM }]}
            >
              {t('pages', { count: selected.edition.page_count })}
            </Text>
          )}
        </ScrollView>
      )}
    </SheetModal>
  );
}

const styles = StyleSheet.create({
  sheetContent: { padding: 16, gap: 12, alignItems: 'center' },
  sheetCover: { width: 120, height: 180, borderRadius: 4 },
  sheetDescription: { lineHeight: 20 },
});

import { StyleSheet, View } from 'react-native';
import { useTranslation } from 'react-i18next';

import SheetModal from '../SheetModal';
import { ListRow } from '../ui/ListRow';
import { ListSection } from '../ui/ListSection';
import { NEXT_STATUS, type LibraryBook } from './libraryShared';

interface BookActionsSheetProps<T extends LibraryBook> {
  /** The book whose actions are shown; null hides the sheet. */
  selected: T | null;
  /** True while offline or while a change to this book is in progress. */
  actionsDisabled: boolean;
  onClose: () => void;
  /** Called after the sheet is dismissed (iOS only), to open another sheet. */
  onHidden?: () => void;
  onAdvance: (item: T) => void;
  onViewDetails: (item: T) => void;
  onRemove: (item: T) => void;
}

/** The ⋯ menu of a My Books row: advance the status, view details, remove. */
export function BookActionsSheet<T extends LibraryBook>({
  selected,
  actionsDisabled,
  onClose,
  onHidden,
  onAdvance,
  onViewDetails,
  onRemove,
}: BookActionsSheetProps<T>) {
  const { t } = useTranslation('my-books');
  const nextStatus = selected ? NEXT_STATUS[selected.status] : undefined;

  return (
    <SheetModal
      visible={!!selected}
      onDismiss={onClose}
      onHidden={onHidden}
      title={selected?.book.title ?? ''}
      subtitle={selected?.book.author}
      closeLabel={t('actions.close')}
      closeA11yLabel={t('actions.close')}
      footer={null}
    >
      {selected ? (
        <View style={styles.body}>
          <ListSection testID="book-actions">
            {nextStatus ? (
              <ListRow
                icon="arrow-forward"
                label={t('markAs', { status: t(`status.${nextStatus}`) })}
                onPress={() => onAdvance(selected)}
                disabled={actionsDisabled}
                chevron={false}
                testID="book-action-advance"
              />
            ) : null}
            <ListRow
              icon="menu-book"
              label={t('actions.viewDetails')}
              onPress={() => onViewDetails(selected)}
              chevron={false}
              testID="book-action-details"
            />
            <ListRow
              icon="delete-outline"
              label={t('actions.remove')}
              onPress={() => onRemove(selected)}
              disabled={actionsDisabled}
              destructive
              chevron={false}
              testID="book-action-remove"
            />
          </ListSection>
        </View>
      ) : null}
    </SheetModal>
  );
}

const styles = StyleSheet.create({
  body: { padding: 16 },
});

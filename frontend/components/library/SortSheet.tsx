import { StyleSheet, View } from 'react-native';
import { useTranslation } from 'react-i18next';

import SheetModal from '../SheetModal';
import { ListRow } from '../ui/ListRow';
import { ListSection } from '../ui/ListSection';
import { SORT_KEYS, type SortKey } from './libraryShared';

interface SortSheetProps {
  visible: boolean;
  value: SortKey;
  onSelect: (key: SortKey) => void;
  onClose: () => void;
}

/** "Sort by" sheet (#933): one row per sort option; the current one shows a check. */
export function SortSheet({ visible, value, onSelect, onClose }: SortSheetProps) {
  const { t } = useTranslation('my-books');
  return (
    <SheetModal
      visible={visible}
      onDismiss={onClose}
      title={t('sort.sheetTitle')}
      closeLabel={t('actions.close')}
      closeA11yLabel={t('actions.close')}
      footer={null}
    >
      <View style={styles.body}>
        <ListSection testID="sort-options" accessibilityRole="radiogroup">
          {SORT_KEYS.map((key) => (
            <ListRow
              key={key}
              label={t(`sort.${key}`)}
              selected={key === value}
              chevron={false}
              onPress={() => onSelect(key)}
              testID={`sort-option-${key}`}
            />
          ))}
        </ListSection>
      </View>
    </SheetModal>
  );
}

const styles = StyleSheet.create({
  body: { padding: 16 },
});

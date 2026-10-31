import { StyleSheet, TextInput, View } from 'react-native';
import { MaterialIcons } from '@expo/vector-icons';
import { useTranslation } from 'react-i18next';

import { useTheme } from '../../hooks/useTheme';
import { useLibraryAccent } from './useLibraryAccent';
import { SCREEN_PADDING } from './libraryShared';

interface LibrarySearchBarProps {
  value: string;
  onChangeText: (text: string) => void;
}

/** Title/author filter box; pads below the floating header in dark mode. */
export function LibrarySearchBar({ value, onChangeText }: LibrarySearchBarProps) {
  const { theme } = useTheme();
  // Reads the `my-books` namespace (My Books only).
  const { t } = useTranslation('my-books');
  const { topPad } = useLibraryAccent();
  return (
    <View style={[styles.searchRow, { paddingTop: 12 + topPad }]}>
      <View style={[styles.searchWrap, { backgroundColor: theme.colors.surfaceContainerHighest }]}>
        <MaterialIcons
          name="search"
          size={20}
          color={theme.colors.outline}
          style={styles.searchIcon}
        />
        <TextInput
          style={[
            styles.searchInput,
            { color: theme.colors.onSurface, fontSize: theme.typography.fontSizeBase },
          ]}
          value={value}
          onChangeText={onChangeText}
          placeholder={t('searchPlaceholder')}
          placeholderTextColor={theme.colors.outline}
          returnKeyType="search"
          accessibilityLabel={t('searchA11y')}
        />
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  searchRow: { paddingHorizontal: SCREEN_PADDING, paddingBottom: 4 },
  searchWrap: {
    flexDirection: 'row',
    alignItems: 'center',
    borderRadius: 12,
    paddingHorizontal: 12,
    height: 48,
  },
  searchIcon: { marginRight: 8 },
  searchInput: { flex: 1, height: '100%' },
});

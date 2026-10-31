import { Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { useTranslation } from 'react-i18next';

import { useTheme } from '../../hooks/useTheme';
import { useLibraryAccent } from './useLibraryAccent';
import { SCREEN_PADDING, STATUS_TAB_KEYS, type Status } from './libraryShared';

interface StatusTabsProps {
  activeTab: Status;
  onChange: (tab: Status) => void;
}

/** Horizontal status filter tabs (All / Wishlisted / Purchased / ...). */
export function StatusTabs({ activeTab, onChange }: StatusTabsProps) {
  const { theme } = useTheme();
  // Reads the `my-books` namespace (My Books only).
  const { t } = useTranslation('my-books');
  const { activeColor, onActiveColor } = useLibraryAccent();
  return (
    <ScrollView
      horizontal
      showsHorizontalScrollIndicator={false}
      contentContainerStyle={styles.tabsContainer}
    >
      <View style={[styles.tabsTrack, { backgroundColor: theme.colors.surfaceContainerLow }]}>
        {STATUS_TAB_KEYS.map((key) => {
          const active = activeTab === key;
          const label = t(`statusTab.${key}`);
          return (
            <Pressable
              key={key}
              style={[styles.tab, active && { backgroundColor: activeColor }]}
              onPress={() => onChange(key)}
              accessibilityRole="button"
              accessibilityLabel={t('filterBy', { label })}
              accessibilityState={{ selected: active }}
            >
              <Text
                style={[
                  styles.tabText,
                  {
                    color: active ? onActiveColor : theme.colors.secondary,
                    fontSize: theme.typography.fontSizeSM,
                  },
                ]}
              >
                {label}
              </Text>
            </Pressable>
          );
        })}
      </View>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  tabsContainer: { paddingHorizontal: SCREEN_PADDING, paddingVertical: 8 },
  tabsTrack: {
    flexDirection: 'row',
    borderRadius: 12,
    padding: 4,
    gap: 2,
  },
  tab: {
    paddingHorizontal: 16,
    paddingVertical: 8,
    borderRadius: 10,
    minHeight: 36,
    justifyContent: 'center',
  },
  tabText: { fontWeight: '600' },
});

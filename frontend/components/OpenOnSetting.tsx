import { useState } from 'react';
import { Modal, Pressable, StyleSheet, Text, View } from 'react-native';
import { MaterialIcons } from '@expo/vector-icons';
import { useTranslation } from 'react-i18next';

import { useDefaultTabSetting } from '../hooks/useDefaultTabSetting';
import { useTheme } from '../hooks/useTheme';
import { DEFAULT_TABS, type DefaultTab } from '../lib/defaultTab';
import { fontFor } from '../theme/typography';

// The settings-namespace label of each "Open on" choice.
const OPTION_KEYS = {
  scan: 'openOnScan',
  wishlist: 'openOnWishlist',
  'my-books': 'openOnMyBooks',
} as const satisfies Record<DefaultTab, string>;

/**
 * Settings "Open on" row (#913): shows the screen the app opens on and opens
 * a picker (Scan, Wishlist, My Books). The choice is saved on the server;
 * `useDefaultTabSetting` owns the optimistic update and the rollback.
 */
export function OpenOnSetting() {
  const { theme } = useTheme();
  const { colors } = theme;
  const { t } = useTranslation('settings');
  const { defaultTab, choose, saving } = useDefaultTabSetting();
  const [open, setOpen] = useState(false);
  const value = t(OPTION_KEYS[defaultTab]);

  function pick(tab: DefaultTab) {
    setOpen(false);
    void choose(tab);
  }

  return (
    <>
      <Pressable
        onPress={() => setOpen(true)}
        // A save in flight would silently drop a second pick, so the row is
        // inert until it settles.
        disabled={saving}
        style={styles.row}
        accessibilityRole="button"
        accessibilityLabel={t('openOnA11y', { value })}
        accessibilityHint={t('openOnHint')}
        accessibilityState={{ busy: saving, disabled: saving }}
        testID="settings-open-on"
      >
        <Text
          style={[styles.label, { color: colors.text, fontSize: theme.typography.fontSizeBase }]}
        >
          {t('openOn')}
        </Text>
        <Text
          style={{ color: colors.textSecondary, fontSize: theme.typography.fontSizeBase }}
          testID="settings-open-on-value"
        >
          {value}
        </Text>
      </Pressable>
      <Modal visible={open} transparent animationType="fade" onRequestClose={() => setOpen(false)}>
        <View style={[styles.overlay, { backgroundColor: colors.scrim }]}>
          <Pressable
            style={StyleSheet.absoluteFill}
            onPress={() => setOpen(false)}
            accessible={false}
            importantForAccessibility="no"
            aria-hidden
            testID="open-on-backdrop"
          />
          <View
            style={[styles.card, { backgroundColor: colors.surface, borderColor: colors.outline }]}
            accessibilityRole="radiogroup"
          >
            <Text style={[styles.title, { color: colors.onSurface }]} accessibilityRole="header">
              {t('openOnPickerTitle')}
            </Text>
            {DEFAULT_TABS.map((tab) => {
              const checked = tab === defaultTab;
              return (
                <Pressable
                  key={tab}
                  onPress={() => pick(tab)}
                  style={styles.option}
                  accessibilityRole="radio"
                  accessibilityLabel={t(OPTION_KEYS[tab])}
                  accessibilityState={{ checked }}
                  testID={`open-on-option-${tab}`}
                >
                  <Text style={[styles.optionText, { color: colors.onSurface }]}>
                    {t(OPTION_KEYS[tab])}
                  </Text>
                  {checked ? <MaterialIcons name="check" size={22} color={colors.primary} /> : null}
                </Pressable>
              );
            })}
            <Pressable
              onPress={() => setOpen(false)}
              style={styles.cancel}
              accessibilityRole="button"
              testID="open-on-cancel"
            >
              <Text style={[styles.cancelText, { color: colors.primary }]}>
                {t('openOnCancel')}
              </Text>
            </Pressable>
          </View>
        </View>
      </Modal>
    </>
  );
}

const styles = StyleSheet.create({
  row: {
    minHeight: 44,
    minWidth: 44,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 8,
    marginTop: 16,
    paddingHorizontal: 16,
  },
  label: { fontFamily: fontFor('600') },
  overlay: { flex: 1, justifyContent: 'center', padding: 24 },
  card: {
    borderRadius: 14,
    borderWidth: StyleSheet.hairlineWidth,
    paddingVertical: 8,
    alignSelf: 'center',
    width: '100%',
    maxWidth: 360,
  },
  title: { fontSize: 18, fontFamily: fontFor('700', 'serif'), padding: 16, paddingBottom: 8 },
  option: {
    minHeight: 48,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: 16,
  },
  optionText: { fontSize: 16, fontFamily: fontFor('400') },
  cancel: { minHeight: 44, justifyContent: 'center', alignItems: 'center', marginTop: 4 },
  cancelText: { fontSize: 16, fontFamily: fontFor('600') },
});

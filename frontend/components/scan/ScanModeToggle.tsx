import React from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';
import { useTranslation } from 'react-i18next';

import { useTheme } from '../../hooks/useTheme';

export type InputMode = 'camera' | 'search';

interface Props {
  inputMode: InputMode;
  onChange: (mode: InputMode) => void;
  /** Active tab background (gold tertiary in dark mode, primary in light). */
  activeColor: string;
  onActiveColor: string;
}

/** Camera / Search segmented control at the top of the Scan screen. */
export function ScanModeToggle({ inputMode, onChange, activeColor, onActiveColor }: Props) {
  const { theme } = useTheme();
  const { t } = useTranslation('scan');
  return (
    <View style={[styles.modeToggle, { backgroundColor: theme.colors.surfaceContainerLow }]}>
      <Pressable
        style={[styles.modeTab, inputMode === 'camera' && { backgroundColor: activeColor }]}
        onPress={() => onChange('camera')}
        accessibilityRole="button"
        accessibilityLabel={t('cameraModeLabel')}
        accessibilityState={{ selected: inputMode === 'camera' }}
      >
        <Text
          style={[
            styles.modeTabText,
            { color: inputMode === 'camera' ? onActiveColor : theme.colors.secondary },
          ]}
        >
          {t('cameraTab')}
        </Text>
      </Pressable>
      <Pressable
        style={[styles.modeTab, inputMode === 'search' && { backgroundColor: activeColor }]}
        onPress={() => onChange('search')}
        accessibilityRole="button"
        accessibilityLabel={t('searchModeLabel')}
        accessibilityState={{ selected: inputMode === 'search' }}
      >
        <Text
          style={[
            styles.modeTabText,
            { color: inputMode === 'search' ? onActiveColor : theme.colors.secondary },
          ]}
        >
          {t('searchTab')}
        </Text>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  modeToggle: {
    flexDirection: 'row',
    borderRadius: 12,
    overflow: 'hidden',
    margin: 16,
    padding: 4,
  },
  modeTab: {
    paddingHorizontal: 24,
    paddingVertical: 8,
    borderRadius: 10,
    minHeight: 44,
    justifyContent: 'center',
  },
  modeTabText: {
    fontWeight: '600',
    fontSize: 14,
  },
});

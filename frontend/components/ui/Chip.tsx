import React from 'react';
import { Pressable, StyleSheet, type StyleProp, type ViewStyle } from 'react-native';

import { useTheme } from '../../hooks/useTheme';
import { AppText } from './AppText';

export interface ChipProps {
  label: string;
  selected?: boolean;
  /** Optional count shown after the label. */
  count?: number;
  onPress: () => void;
  disabled?: boolean;
  /** Overrides the spoken label (defaults to label plus count). */
  accessibilityLabel?: string;
  testID?: string;
  style?: StyleProp<ViewStyle>;
}

/** Filter chip. Selected = primary fill; unselected = outlined. */
export function Chip({
  label,
  selected = false,
  count,
  onPress,
  disabled = false,
  accessibilityLabel,
  testID,
  style,
}: ChipProps) {
  const { theme } = useTheme();
  const text = count === undefined ? label : `${label} ${count}`;
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled}
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel ?? text}
      accessibilityState={{ selected, disabled }}
      testID={testID}
      style={({ pressed }) => [
        styles.base,
        {
          backgroundColor: selected ? theme.colors.primary : 'transparent',
          borderColor: selected ? theme.colors.primary : theme.colors.outlineField,
          borderRadius: theme.radius.chip,
          opacity: disabled ? 0.4 : pressed ? 0.85 : 1,
        },
        style,
      ]}
    >
      <AppText variant="secondary" color={selected ? 'onPrimary' : 'onSurface'}>
        {text}
      </AppText>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  base: {
    minHeight: 44,
    paddingHorizontal: 14,
    borderWidth: 1,
    alignItems: 'center',
    justifyContent: 'center',
  },
});

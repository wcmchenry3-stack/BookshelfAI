import React from 'react';
import { Pressable, StyleSheet, type StyleProp, type ViewStyle } from 'react-native';
import { MaterialIcons } from '@expo/vector-icons';

import { buttonA11yProps, pressOpacity } from './buttonBehaviour';
import { useTheme } from '../../hooks/useTheme';
import type { ColorKey } from '../../theme/light';
import type { IconName } from './Button';

export interface IconButtonProps {
  icon: IconName;
  onPress: () => void;
  /** Required: an icon alone has no visible text. Callers pass translated copy. */
  accessibilityLabel: string;
  disabled?: boolean;
  /** Theme colour role for the icon. Defaults to `onSurfaceVariant`. */
  color?: ColorKey;
  size?: number;
  testID?: string;
  style?: StyleProp<ViewStyle>;
}

/** 44x44 icon-only button. */
export function IconButton({
  icon,
  onPress,
  accessibilityLabel,
  disabled = false,
  color = 'onSurfaceVariant',
  size = 24,
  testID,
  style,
}: IconButtonProps) {
  const { theme } = useTheme();
  return (
    <Pressable
      onPress={onPress}
      {...buttonA11yProps(accessibilityLabel, disabled, testID)}
      style={({ pressed }) => [
        styles.base,
        { opacity: pressOpacity(disabled, pressed, 0.6) },
        style,
      ]}
    >
      <MaterialIcons name={icon} size={size} color={theme.colors[color]} />
    </Pressable>
  );
}

const styles = StyleSheet.create({
  base: { width: 44, height: 44, alignItems: 'center', justifyContent: 'center' },
});

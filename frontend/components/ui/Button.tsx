import React from 'react';
import {
  ActivityIndicator,
  Pressable,
  StyleSheet,
  View,
  type StyleProp,
  type ViewStyle,
} from 'react-native';
import { MaterialIcons } from '@expo/vector-icons';

import { useTheme } from '../../hooks/useTheme';
import type { ColorKey } from '../../theme/light';
import { AppText } from './AppText';

type ButtonVariant = 'primary' | 'secondary' | 'text' | 'destructive';
export type IconName = React.ComponentProps<typeof MaterialIcons>['name'];

export interface ButtonProps {
  /** Visible label. Callers pass translated copy. */
  label: string;
  onPress: () => void;
  variant?: ButtonVariant;
  disabled?: boolean;
  /** Shows a spinner in place of the icon and blocks presses. */
  loading?: boolean;
  /** Optional leading icon (MaterialIcons name). */
  icon?: IconName;
  /** Overrides the spoken label (defaults to `label`). */
  accessibilityLabel?: string;
  testID?: string;
  style?: StyleProp<ViewStyle>;
}

const COLOURS: Record<ButtonVariant, { bg: ColorKey | null; fg: ColorKey }> = {
  primary: { bg: 'primary', fg: 'onPrimary' },
  secondary: { bg: 'primarySoft', fg: 'primary' },
  text: { bg: null, fg: 'primary' },
  destructive: { bg: 'danger', fg: 'onDanger' },
};

/** Themed button. Primary, secondary and destructive are 52px tall; text keeps a 44px minimum. */
export function Button({
  label,
  onPress,
  variant = 'primary',
  disabled = false,
  loading = false,
  icon,
  accessibilityLabel,
  testID,
  style,
}: ButtonProps) {
  const { theme } = useTheme();
  const { bg, fg } = COLOURS[variant];
  const inactive = disabled || loading;
  const fgColour = theme.colors[fg];
  return (
    <Pressable
      onPress={onPress}
      disabled={inactive}
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel ?? label}
      accessibilityState={{ disabled: inactive, busy: loading }}
      testID={testID}
      style={({ pressed }) => [
        styles.base,
        variant === 'text' ? styles.textHeight : styles.solidHeight,
        {
          backgroundColor: bg ? theme.colors[bg] : 'transparent',
          borderRadius: theme.radius.button,
          opacity: disabled ? 0.4 : pressed ? 0.85 : 1,
        },
        style,
      ]}
    >
      {loading ? (
        <ActivityIndicator
          size="small"
          color={fgColour}
          testID={testID ? `${testID}-spinner` : undefined}
        />
      ) : icon ? (
        <MaterialIcons name={icon} size={20} color={fgColour} />
      ) : null}
      {loading || icon ? <View style={styles.gap} /> : null}
      <AppText variant="bodyStrong" color={fg}>
        {label}
      </AppText>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  base: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    paddingHorizontal: 20,
  },
  solidHeight: { minHeight: 52 },
  textHeight: { minHeight: 44 },
  gap: { width: 8 },
});

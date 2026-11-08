import React from 'react';
import { Pressable, StyleSheet, View } from 'react-native';
import { MaterialIcons } from '@expo/vector-icons';

import { useTheme } from '../../hooks/useTheme';
import { AppText } from './AppText';
import type { IconName } from './Button';
import { useFocusRing } from './useFocusRing';

/** Icons next to a label are decoration: the row's own label says it all. */
const DECORATIVE = {
  accessibilityElementsHidden: true,
  importantForAccessibility: 'no-hide-descendants',
  'aria-hidden': true,
} as const;

export interface ListRowProps {
  label: string;
  /** Optional second line under the label. */
  hint?: string;
  /** Optional trailing value text. */
  value?: string;
  /** Icon shown in a tinted tile at the start of the row. */
  icon?: IconName;
  /** Shows a trailing chevron. Defaults to true when `onPress` is set. */
  chevron?: boolean;
  /** Red label and icon for destructive actions (sign out, delete). */
  destructive?: boolean;
  onPress?: () => void;
  disabled?: boolean;
  /**
   * Marks the row as a choice in a single-select list: the selected row shows a check
   * (in place of the chevron) and is a `radio` reporting checked state through both
   * `accessibilityState.checked` (native) and `aria-checked` (react-native-web ignores
   * `accessibilityState`, and drops `aria-selected` on buttons). Put such rows in a `radiogroup`.
   */
  selected?: boolean;
  accessibilityLabel?: string;
  testID?: string;
}

/** A row in a `ListSection`: at least 52px tall. Pressable only when `onPress` is set. */
export function ListRow({
  label,
  hint,
  value,
  icon,
  chevron,
  destructive = false,
  onPress,
  disabled = false,
  selected,
  accessibilityLabel,
  testID,
}: ListRowProps) {
  const { theme } = useTheme();
  const { focusProps, focusStyle } = useFocusRing({ inset: true });
  const showChevron = !selected && (chevron ?? onPress !== undefined);
  const fg = destructive ? 'danger' : 'onSurface';
  const spoken = accessibilityLabel ?? [label, value, hint].filter(Boolean).join(', ');

  const content = (
    <>
      {icon ? (
        <View
          style={[
            styles.tile,
            { backgroundColor: destructive ? theme.colors.dangerSoft : theme.colors.primarySoft },
          ]}
        >
          <MaterialIcons
            name={icon}
            testID={testID ? `${testID}-icon` : 'list-row-icon'}
            size={20}
            color={destructive ? theme.colors.danger : theme.colors.primary}
          />
        </View>
      ) : null}
      <View style={styles.text}>
        <AppText variant="body" color={fg}>
          {label}
        </AppText>
        {hint ? (
          <AppText variant="meta" color="onSurfaceVariant">
            {hint}
          </AppText>
        ) : null}
      </View>
      {value ? (
        <AppText variant="secondary" color="onSurfaceVariant" style={styles.value}>
          {value}
        </AppText>
      ) : null}
      {selected ? (
        <MaterialIcons
          name="check"
          testID={testID ? `${testID}-check` : 'list-row-check'}
          size={22}
          color={theme.colors.primary}
          {...DECORATIVE}
        />
      ) : null}
      {showChevron ? (
        <MaterialIcons
          name="chevron-right"
          testID={testID ? `${testID}-chevron` : 'list-row-chevron'}
          size={22}
          color={theme.colors.onSurfaceVariant}
        />
      ) : null}
    </>
  );

  if (!onPress) {
    return (
      <View style={styles.row} testID={testID} accessible>
        {content}
      </View>
    );
  }
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled}
      accessibilityRole={selected === undefined ? 'button' : 'radio'}
      accessibilityLabel={spoken}
      accessibilityState={selected === undefined ? { disabled } : { disabled, checked: selected }}
      aria-checked={selected}
      testID={testID}
      {...focusProps}
      style={({ pressed }) => [
        styles.row,
        { opacity: disabled ? 0.4 : pressed ? 0.7 : 1 },
        focusStyle,
      ]}
    >
      {content}
    </Pressable>
  );
}

const styles = StyleSheet.create({
  row: {
    minHeight: 52,
    flexDirection: 'row',
    alignItems: 'center',
    paddingHorizontal: 16,
    paddingVertical: 8,
    gap: 12,
  },
  tile: { width: 32, height: 32, borderRadius: 8, alignItems: 'center', justifyContent: 'center' },
  text: { flex: 1 },
  value: { marginLeft: 8 },
});

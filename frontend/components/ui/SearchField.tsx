import React from 'react';
import {
  Pressable,
  StyleSheet,
  TextInput,
  View,
  type StyleProp,
  type ViewStyle,
} from 'react-native';
import { MaterialIcons } from '@expo/vector-icons';

import { useTheme } from '../../hooks/useTheme';
import { fontFor } from '../../theme/typography';

export interface SearchFieldProps {
  value: string;
  onChangeText: (text: string) => void;
  placeholder: string;
  /** Spoken label of the input. Callers pass translated copy. */
  accessibilityLabel: string;
  /** Spoken label of the clear button, shown when `value` is not empty. */
  clearAccessibilityLabel: string;
  onSubmitEditing?: () => void;
  testID?: string;
  style?: StyleProp<ViewStyle>;
}

/** 40px search field: field fill, 1px outlineField border (WCAG 1.4.11), clear button. */
export function SearchField({
  value,
  onChangeText,
  placeholder,
  accessibilityLabel,
  clearAccessibilityLabel,
  onSubmitEditing,
  testID,
  style,
}: SearchFieldProps) {
  const { theme } = useTheme();
  return (
    <View
      style={[
        styles.wrap,
        {
          backgroundColor: theme.colors.field,
          borderColor: theme.colors.outlineField,
          borderRadius: theme.radius.field,
        },
        style,
      ]}
    >
      <MaterialIcons name="search" size={20} color={theme.colors.onSurfaceVariant} />
      <TextInput
        style={[styles.input, { color: theme.colors.onSurface, fontFamily: fontFor('400') }]}
        value={value}
        onChangeText={onChangeText}
        placeholder={placeholder}
        placeholderTextColor={theme.colors.onSurfaceVariant}
        returnKeyType="search"
        onSubmitEditing={onSubmitEditing}
        accessibilityLabel={accessibilityLabel}
        testID={testID}
      />
      {value.length > 0 ? (
        <Pressable
          onPress={() => onChangeText('')}
          accessibilityRole="button"
          accessibilityLabel={clearAccessibilityLabel}
          hitSlop={{ top: 2, bottom: 2, left: 4, right: 4 }}
          testID={testID ? `${testID}-clear` : undefined}
          style={styles.clear}
        >
          <MaterialIcons name="close" size={18} color={theme.colors.onSurfaceVariant} />
        </Pressable>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: {
    flexDirection: 'row',
    alignItems: 'center',
    height: 40,
    borderWidth: 1,
    paddingLeft: 10,
    gap: 8,
  },
  input: { flex: 1, height: '100%', fontSize: 16, paddingVertical: 0 },
  // 38px tall inside the 40px field (1px border each side); hitSlop widens it to 42px
  // vertically and 48px wide, the most the fixed 40px field height allows.
  clear: { width: 40, height: 38, alignItems: 'center', justifyContent: 'center' },
});

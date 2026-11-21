import React from 'react';
import {
  Platform,
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
import { useFocusRing } from './useFocusRing';

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
  const inputRef = React.useRef<TextInput>(null);
  const showClear = value.length > 0;
  // Text inputs show focus on click too, so the field ring appears on any focus.
  const field = useFocusRing({ always: true });
  const clearRing = useFocusRing();
  return (
    // The 44px wrapper owns the clear target (Android clips hitSlop outside the parent);
    // the visible field is a 40px box inside it.
    <View style={[styles.outer, style]}>
      <View
        testID={testID ? `${testID}-field` : undefined}
        style={[
          styles.wrap,
          {
            backgroundColor: theme.colors.field,
            borderColor: theme.colors.outlineField,
            borderRadius: theme.radius.field,
          },
          field.focusStyle,
        ]}
      >
        <MaterialIcons name="search" size={20} color={theme.colors.onSurfaceVariant} />
        <TextInput
          ref={inputRef}
          style={[
            styles.input,
            { color: theme.colors.onSurface, fontFamily: fontFor('400') },
            showClear && styles.inputWithClear,
            // The field container shows the ring; drop the browser's second outline.
            Platform.OS === 'web' && styles.inputNoOutline,
          ]}
          value={value}
          onChangeText={onChangeText}
          placeholder={placeholder}
          placeholderTextColor={theme.colors.onSurfaceVariant}
          returnKeyType="search"
          autoCorrect={false}
          autoCapitalize="none"
          onSubmitEditing={onSubmitEditing}
          accessibilityLabel={accessibilityLabel}
          testID={testID}
          {...field.focusProps}
        />
      </View>
      {showClear ? (
        <Pressable
          onPress={() => {
            onChangeText('');
            inputRef.current?.focus();
          }}
          accessibilityRole="button"
          accessibilityLabel={clearAccessibilityLabel}
          testID={testID ? `${testID}-clear` : 'search-clear'}
          {...clearRing.focusProps}
          style={[styles.clear, clearRing.focusStyle]}
        >
          <MaterialIcons name="close" size={18} color={theme.colors.onSurfaceVariant} />
        </Pressable>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  outer: { minHeight: 44, justifyContent: 'center' },
  wrap: {
    flexDirection: 'row',
    alignItems: 'center',
    minHeight: 40,
    borderWidth: 1,
    paddingLeft: 10,
    gap: 8,
  },
  input: { flex: 1, minHeight: 38, fontSize: 16, paddingVertical: 0 },
  inputWithClear: { paddingRight: 44 },
  inputNoOutline: { outlineWidth: 0 },
  clear: {
    position: 'absolute',
    right: 0,
    top: 0,
    width: 44,
    height: 44,
    alignItems: 'center',
    justifyContent: 'center',
  },
});

import { Pressable, StyleSheet, View } from 'react-native';
import { MaterialIcons } from '@expo/vector-icons';

import { AppText } from '../ui/AppText';
import { useTheme } from '../../hooks/useTheme';
import { fontFor } from '../../theme/typography';

interface BoughtChipProps {
  label: string;
  accessibilityLabel: string;
  onPress: () => void;
  disabled?: boolean;
  testID?: string;
}

/**
 * The tonal "Bought" chip on a Wishlist row: a 32px pill (`primarySoft` fill,
 * `primary` text and check icon) inside a 44px-tall touch target.
 */
export function BoughtChip({
  label,
  accessibilityLabel,
  onPress,
  disabled = false,
  testID,
}: BoughtChipProps) {
  const { theme } = useTheme();
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled}
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel}
      accessibilityState={{ disabled }}
      testID={testID}
      style={({ pressed }) => [styles.target, { opacity: disabled ? 0.4 : pressed ? 0.7 : 1 }]}
    >
      <View
        style={[
          styles.pill,
          { backgroundColor: theme.colors.primarySoft, borderRadius: theme.radius.chip },
        ]}
      >
        <MaterialIcons name="check" size={16} color={theme.colors.primary} />
        <AppText variant="meta" color="primary" style={styles.label}>
          {label}
        </AppText>
      </View>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  target: { minHeight: 44, minWidth: 44, justifyContent: 'center' },
  pill: {
    height: 32,
    paddingHorizontal: 12,
    flexDirection: 'row',
    alignItems: 'center',
    gap: 4,
  },
  label: { fontFamily: fontFor('600') },
});

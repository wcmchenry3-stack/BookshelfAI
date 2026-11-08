import { Pressable, StyleSheet, View } from 'react-native';
import { MaterialIcons } from '@expo/vector-icons';

import { AppText } from '../ui/AppText';
import { buttonA11yProps, pressOpacity } from '../ui/buttonBehaviour';
import { useFocusRing } from '../ui/useFocusRing';
import { useTheme } from '../../hooks/useTheme';
import { fontFor } from '../../theme/typography';

export interface TonalChipProps {
  label: string;
  accessibilityLabel: string;
  onPress: () => void;
  /** MaterialIcons name of the leading icon. */
  icon: React.ComponentProps<typeof MaterialIcons>['name'];
  disabled?: boolean;
  testID?: string;
}

/**
 * A tonal chip at the end of a library row: a 32px pill (`primarySoft` fill,
 * `primary` text and icon) inside a 44px-tall touch target. The Wishlist
 * "Bought" chip and the Trash "Restore" chip are both this.
 */
export function TonalChip({
  label,
  accessibilityLabel,
  onPress,
  icon,
  disabled = false,
  testID,
}: TonalChipProps) {
  const { theme } = useTheme();
  const { focusProps, focusStyle } = useFocusRing();
  return (
    <Pressable
      onPress={onPress}
      {...focusProps}
      {...buttonA11yProps(accessibilityLabel, disabled, testID)}
      style={({ pressed }) => [
        styles.target,
        focusStyle,
        { opacity: pressOpacity(disabled, pressed, 0.7) },
      ]}
    >
      <View
        style={[
          styles.pill,
          { backgroundColor: theme.colors.primarySoft, borderRadius: theme.radius.chip },
        ]}
      >
        <MaterialIcons name={icon} size={16} color={theme.colors.primary} />
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

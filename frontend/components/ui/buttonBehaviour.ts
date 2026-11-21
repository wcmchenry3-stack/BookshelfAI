/**
 * Shared Pressable behaviour for small buttons (IconButton, the Wishlist
 * BoughtChip): button role, label, disabled state, and press feedback.
 */
export function buttonA11yProps(accessibilityLabel: string, disabled: boolean, testID?: string) {
  return {
    accessibilityRole: 'button' as const,
    accessibilityLabel,
    accessibilityState: { disabled },
    disabled,
    testID,
  };
}

/** Opacity for a button: dimmed when disabled, lightly faded while pressed. */
export function pressOpacity(disabled: boolean, pressed: boolean, pressedOpacity: number) {
  if (disabled) return 0.4;
  return pressed ? pressedOpacity : 1;
}

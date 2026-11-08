import React from 'react';
import { Platform, type ViewStyle } from 'react-native';

import { useTheme } from '../../hooks/useTheme';

/**
 * Keyboard focus ring for web (#923, WCAG 2.4.7). One shared mechanism for every
 * Pressable-based control in `components/ui` (and TonalChip).
 *
 * Focus-visible semantics: the ring shows only when the element took focus while the last
 * input was the keyboard. A module-level listener (installed once, web only, on first use)
 * tracks the last input modality: keydown means keyboard, pointer/mouse/touch down means
 * pointer. The check happens in `onFocus`, so a click that focuses a control never draws a
 * ring. This is the approach of the `focus-visible` polyfill and is robust in
 * react-native-web, where a `:focus-visible` rule cannot be set from a style object.
 * Native is unchanged (the ring is web only).
 */
const RING_WIDTH = 2;

let keyboardModality = false;
let installed = false;

function installModalityListeners() {
  if (installed || typeof document === 'undefined') return;
  installed = true;
  document.addEventListener(
    'keydown',
    (e: KeyboardEvent) => {
      // Shortcuts with a modifier do not move focus.
      if (e.metaKey || e.altKey || e.ctrlKey) return;
      keyboardModality = true;
    },
    true
  );
  const pointer = () => {
    keyboardModality = false;
  };
  for (const type of ['pointerdown', 'mousedown', 'touchstart']) {
    document.addEventListener(type, pointer, true);
  }
}

export interface FocusRingOptions {
  /**
   * Show the ring on any focus, pointer included. For text inputs, which conventionally
   * show focus on click too (`:focus-visible` matches them on click as well).
   */
  always?: boolean;
  /** Draw the ring inside the element, for rows that sit in a clipping (overflow hidden) card. */
  inset?: boolean;
}

/** `onFocus`/`onBlur` to spread on the control and the ring style to append to its style. */
export function useFocusRing({ always = false, inset = false }: FocusRingOptions = {}) {
  const { theme } = useTheme();
  const [visible, setVisible] = React.useState(false);
  const isWeb = Platform.OS === 'web';

  React.useEffect(() => {
    if (isWeb) installModalityListeners();
  }, [isWeb]);

  const onFocus = React.useCallback(() => {
    if (isWeb) setVisible(always || keyboardModality);
  }, [isWeb, always]);
  const onBlur = React.useCallback(() => setVisible(false), []);

  const focusStyle: ViewStyle | undefined = visible
    ? {
        outlineStyle: 'solid',
        outlineWidth: RING_WIDTH,
        outlineColor: theme.colors.primary,
        outlineOffset: inset ? -RING_WIDTH : 2,
      }
    : undefined;

  return { focusProps: { onFocus, onBlur }, focusStyle };
}

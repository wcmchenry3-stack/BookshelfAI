import { useEffect, useRef } from 'react';
import { AccessibilityInfo, findNodeHandle, Platform, type TextInstance } from 'react-native';

/**
 * Moves screen-reader focus to a modal's title when it opens (native only).
 * Returns the ref to put on the title. Shared by SheetModal and the AI
 * consent prompt (#1023).
 */
export function useModalTitleFocus(visible: boolean) {
  const titleRef = useRef<TextInstance>(null);
  useEffect(() => {
    // react-native-web's findNodeHandle always throws; the web Modal already
    // moves focus into the dialog, so native focusing is skipped there.
    if (!visible || Platform.OS === 'web') return;
    // Wait a beat so the node is mounted inside the Modal before focusing.
    const timer = setTimeout(() => {
      const node = titleRef.current ? findNodeHandle(titleRef.current) : null;
      if (node) AccessibilityInfo.setAccessibilityFocus(node);
    }, 100);
    return () => clearTimeout(timer);
  }, [visible]);
  return titleRef;
}

import { useCallback, useEffect, useRef, useState } from 'react';
import { Platform } from 'react-native';

/** How long iOS waits for the other modal's onDismiss before opening anyway. */
export const IOS_DISMISS_FALLBACK_MS = 800;

/**
 * Hands the screen from one modal to the next without overlap.
 *
 * iOS can fail to present a page-sheet Modal in the same commit that unmounts
 * another Modal. While `blocked` is true (the other modal is wanted or open)
 * `clear` is false; once `blocked` ends, `clear` turns true only after the
 * other modal has finished dismissing:
 *  - iOS: when its `onDismiss` fires (pass `notifyDismissed` to the Modal), or
 *    after IOS_DISMISS_FALLBACK_MS if that never fires;
 *  - Android and web, which have no `onDismiss`: on the next macrotask,
 *    after the unmount has been flushed.
 * With no handoff pending, `clear` is true and nothing is delayed.
 */
export function useModalHandoff(blocked: boolean) {
  // True from the moment `blocked` starts until the handoff completes.
  const [waiting, setWaiting] = useState(blocked);
  if (blocked && !waiting) setWaiting(true);
  const blockedRef = useRef(blocked);

  useEffect(() => {
    blockedRef.current = blocked;
    if (blocked || !waiting) return;
    const finish = () => setWaiting(false);
    // Android and web never report the dismissal: a macrotask later, the
    // unmount has been committed and flushed.
    const timer = setTimeout(finish, Platform.OS === 'ios' ? IOS_DISMISS_FALLBACK_MS : 0);
    return () => clearTimeout(timer);
  }, [blocked, waiting]);

  /** Pass as the blocking Modal's onDismiss (iOS only fires it). */
  const notifyDismissed = useCallback(() => {
    // An onDismiss while still blocked is a remount (new `key`), not the handoff.
    if (!blockedRef.current && Platform.OS === 'ios') setWaiting(false);
  }, []);

  return { clear: !blocked && !waiting, notifyDismissed };
}

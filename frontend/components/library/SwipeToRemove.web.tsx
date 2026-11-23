import type { SwipeToRemoveProps } from './swipeToRemoveShared';

/**
 * Web has no swipe (#935): the row renders as is and removal goes through the
 * detail sheet. A separate file so the web bundle never imports gesture-handler,
 * Reanimated or Worklets (see `components/GestureRoot.web.tsx`).
 */
export function SwipeToRemove({ children }: SwipeToRemoveProps) {
  return <>{children}</>;
}

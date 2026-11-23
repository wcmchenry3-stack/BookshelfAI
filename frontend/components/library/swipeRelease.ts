/**
 * Decides whether a released swipe removes the book (#935), from two events
 * that reach JS separately for every swipe:
 *
 *  - `release(distance)`: from the row's observer pan, on the UI thread at the
 *    moment of release, how far the row had travelled towards the panel
 *    (start position + finger travel). Captured before ReanimatedSwipeable's
 *    spring moves the row, so Reduce Motion (the spring target applied at
 *    once) or a busy JS thread cannot change it.
 *  - `outcome('open' | 'close')`: ReanimatedSwipeable's `onSwipeableWillOpen` /
 *    `onSwipeableWillClose`, which its own release handler sends.
 *
 * Both are scheduled onto JS from the same touch-up on the UI thread, in an
 * order gesture-handler does not define, so whichever arrives second settles
 * the swipe: it removes when the row is opening and the release was at least
 * `fullDistance`. `begin()` (the observer's touch-down, which reaches JS after
 * every event of the previous swipe) drops anything left unpaired, e.g. a
 * programmatic close.
 */
type SwipeOutcome = 'open' | 'close';

export interface SwipeReleaseDecider {
  begin: () => void;
  /** Returns true when this event settles the swipe as a full swipe. */
  release: (distance: number) => boolean;
  /** Returns true when this event settles the swipe as a full swipe. */
  outcome: (outcome: SwipeOutcome) => boolean;
}

export function createSwipeReleaseDecider(fullDistance: number): SwipeReleaseDecider {
  let distance: number | null = null;
  let pending: SwipeOutcome | null = null;

  function reset() {
    distance = null;
    pending = null;
  }

  function settle(): boolean {
    if (distance === null || pending === null) return false;
    const full = pending === 'open' && distance >= fullDistance;
    reset();
    return full;
  }

  return {
    begin: reset,
    release(value) {
      distance = value;
      return settle();
    },
    outcome(value) {
      pending = value;
      return settle();
    },
  };
}

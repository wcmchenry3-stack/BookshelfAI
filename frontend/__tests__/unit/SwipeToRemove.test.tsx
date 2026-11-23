/**
 * Swipe to remove (#935). ReanimatedSwipeable is replaced by a stand-in that
 * renders its actions with a real Reanimated shared value as the translation
 * and keeps its props, so a test can call the callbacks gesture-handler would
 * call (will open, will close) and move the live translation. The row's own
 * observer pan (handed to the swipeable as `simultaneousWith`) is real and is
 * driven with gesture-handler's jest utils: its release distance is what
 * decides a full swipe. The gesture itself needs a device (owner check).
 */
import React from 'react';
import { I18nManager, Text } from 'react-native';
import { act, fireEvent, render, renderHook, waitFor } from '@testing-library/react-native';
import * as Haptics from 'expo-haptics';
import { useReducedMotion, type SharedValue } from 'react-native-reanimated';
import type { PanGesture } from 'react-native-gesture-handler';
import { createGestureController } from 'react-native-gesture-handler/jest-utils';

import { SwipeToRemove as WebSwipeToRemove } from '../../components/library/SwipeToRemove.web';
import {
  FULL_SWIPE_DISTANCE,
  REDUCED_MOTION_OPTIONS,
  REMOVE_PANEL_WIDTH,
  SPRING_OPTIONS,
  SWIPE_ACTIVATION_OFFSET,
  SwipeToRemove,
} from '../../components/library/SwipeToRemove';
import { useSingleOpenRow, type SwipeRowGroup } from '../../components/library/swipeToRemoveShared';

interface MockRow {
  props: Record<string, unknown> & {
    enabled?: boolean;
    onSwipeableWillOpen?: (direction: string) => void;
    onSwipeableWillClose?: (direction: string) => void;
    onSwipeableClose?: (direction: string) => void;
    simultaneousWith?: unknown;
    renderLeftActions?: unknown;
    renderRightActions?: unknown;
    animationOptions?: unknown;
  };
  translation: SharedValue<number>;
  close: jest.Mock;
}

jest.mock('react-native-gesture-handler/ReanimatedSwipeable', () => {
  const React = jest.requireActual('react');
  const { View } = jest.requireActual('react-native');
  const { makeMutable } = jest.requireActual('react-native-reanimated');
  const rows: MockRow[] = [];
  function MockSwipeable({ ref, ...props }: MockRow['props'] & { ref: React.Ref<unknown> }) {
    const [row] = React.useState(() => ({
      progress: makeMutable(0),
      translation: makeMutable(0),
      close: jest.fn(),
    }));
    // The latest props, for the test to call the swipe callbacks.
    React.useEffect(() => {
      const index = rows.findIndex((r) => r.close === row.close);
      const entry = { props, translation: row.translation, close: row.close };
      if (index === -1) rows.push(entry);
      else rows[index] = entry;
    });
    React.useImperativeHandle(
      ref,
      () => ({ close: row.close, openLeft: jest.fn(), openRight: jest.fn(), reset: jest.fn() }),
      [row]
    );
    const renderActions = (props.renderRightActions ?? props.renderLeftActions) as (
      ...args: unknown[]
    ) => React.ReactNode;
    return React.createElement(
      View,
      { testID: props.testID },
      renderActions(row.progress, row.translation, {}),
      props.children as React.ReactNode
    );
  }
  return { __esModule: true, default: MockSwipeable, __rows: rows };
});

jest.mock('react-native-reanimated', () => ({
  ...jest.requireActual('react-native-reanimated'),
  useReducedMotion: jest.fn(() => false),
}));

jest.mock('../../hooks/useTheme', () => require('../helpers/screenMocks').themeModule());

const rows: MockRow[] = jest.requireMock('react-native-gesture-handler/ReanimatedSwipeable').__rows;

function Row({
  onRemove,
  disabled,
  group,
  title = 'Dune',
}: {
  onRemove: () => Promise<boolean>;
  disabled?: boolean;
  group?: SwipeRowGroup;
  title?: string;
}) {
  return (
    <SwipeToRemove
      onRemove={onRemove}
      label={`Remove ${title} from wishlist`}
      actionText="Remove"
      disabled={disabled}
      group={group}
    >
      <Text>{title}</Text>
    </SwipeToRemove>
  );
}

/** Moves the row as a drag would and lets Reanimated's reaction and scheduleOnRN run. */
async function dragTo(row: MockRow, x: number) {
  await act(async () => {
    row.translation.value = x;
  });
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 50));
  });
}

/** Lets scheduleOnRN (asynchronous on Reanimated's web build) deliver. */
async function settle() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 50));
  });
}

const PAST = FULL_SWIPE_DISTANCE + 10;

/** The row's observer pan, as the UI thread drives it. */
function finger(row: MockRow) {
  const observer = row.props.simultaneousWith as PanGesture;
  const controller = createGestureController(observer);
  return {
    async down() {
      await act(async () => controller.begin());
      await settle();
    },
    /** Moves `travel` (signed) from the touch-down point and lets go. */
    async release(travel: number) {
      await act(async () => {
        controller.activate({ translationX: travel / 2 });
        controller.end({ translationX: travel });
      });
      await settle();
    },
    /** Moves `travel` and the gesture is cancelled while active (e.g. the list took it). */
    async cancel(travel: number) {
      await act(async () => {
        controller.activate({ translationX: travel / 2 });
        controller.cancel({ translationX: travel });
      });
      await settle();
    },
    /** A touch that never moves far enough to activate (a tap). */
    async tap() {
      await act(async () => {
        controller.begin();
        controller.fail();
      });
      await settle();
    },
  };
}

/** A whole swipe: touch-down, travel, let go. */
async function swipe(row: MockRow, travel: number) {
  const f = finger(row);
  await f.down();
  await f.release(travel);
}

async function willOpen(row: MockRow, direction = 'left') {
  await act(async () => row.props.onSwipeableWillOpen?.(direction));
  await settle();
}

async function willClose(row: MockRow) {
  await act(async () => row.props.onSwipeableWillClose?.('left'));
  await settle();
}

/** Where the row's spring has got to (what the panel mirrors as the live position). */
async function rowAt(row: MockRow, x: number) {
  await act(async () => {
    row.translation.value = x;
  });
  await settle();
}

/** A short swipe that opens the row, then its spring lands on the panel. */
async function openAtRest(row: MockRow) {
  await swipe(row, -REMOVE_PANEL_WIDTH);
  await willOpen(row);
  await rowAt(row, -REMOVE_PANEL_WIDTH);
}

beforeEach(() => {
  rows.length = 0;
  jest.clearAllMocks();
  (useReducedMotion as jest.Mock).mockReturnValue(false);
});

describe('SwipeToRemove (#935)', () => {
  it('reveals an 88px danger panel: a button labelled "Remove {title} from wishlist"', async () => {
    const screen = await render(<Row onRemove={jest.fn()} />);
    const panel = screen.getByRole('button', {
      name: 'Remove Dune from wishlist',
      includeHiddenElements: true,
    });
    expect(panel.props.testID).toBe('swipe-remove-panel');
    expect(REMOVE_PANEL_WIDTH).toBe(88);
    expect(panel).toHaveStyle({ width: 88, backgroundColor: '#B3261E' });
    expect(screen.getByText('Remove', { includeHiddenElements: true })).toBeTruthy();
    expect(screen.getByText('Dune')).toBeTruthy();
  });

  it('hides the panel from screen readers (it stays mounted behind a closed row)', async () => {
    const screen = await render(<Row onRemove={jest.fn()} />);
    expect(screen.queryByRole('button', { name: 'Remove Dune from wishlist' })).toBeNull();
    expect(screen.queryByText('Remove')).toBeNull();
    const wrap = screen.getByTestId('swipe-remove-panel-wrap', { includeHiddenElements: true });
    expect(wrap.props.accessibilityElementsHidden).toBe(true);
    expect(wrap.props.importantForAccessibility).toBe('no-hide-descendants');
    expect(wrap).not.toBeVisible();
  });

  it('pressing the panel removes the book; a removed row is not closed (it unmounts)', async () => {
    const onRemove = jest.fn().mockResolvedValue(true);
    const screen = await render(<Row onRemove={onRemove} />);
    await act(async () => {
      fireEvent.press(screen.getByTestId('swipe-remove-panel', { includeHiddenElements: true }));
    });
    expect(onRemove).toHaveBeenCalledTimes(1);
    expect(rows[0].close).not.toHaveBeenCalled();
  });

  it('closes the row when the panel press did not remove (failed, offline or in flight)', async () => {
    const onRemove = jest.fn().mockResolvedValue(false);
    const screen = await render(<Row onRemove={onRemove} />);
    await act(async () => {
      fireEvent.press(screen.getByTestId('swipe-remove-panel', { includeHiddenElements: true }));
    });
    expect(onRemove).toHaveBeenCalledTimes(1);
    expect(rows[0].close).toHaveBeenCalledTimes(1);
  });

  it('ignores a second panel press while its removal is in flight', async () => {
    let finish: (removed: boolean) => void = () => {};
    const onRemove = jest.fn(() => new Promise<boolean>((resolve) => (finish = resolve)));
    const screen = await render(<Row onRemove={onRemove} />);
    const panel = () => screen.getByTestId('swipe-remove-panel', { includeHiddenElements: true });
    await act(async () => {
      fireEvent.press(panel());
      fireEvent.press(panel());
    });
    expect(onRemove).toHaveBeenCalledTimes(1);
    expect(rows[0].close).not.toHaveBeenCalled();
    await act(async () => finish(true));
    expect(rows[0].close).not.toHaveBeenCalled();
  });

  it('a full swipe removes the book straight away, with a haptic tick at the threshold', async () => {
    const onRemove = jest.fn().mockResolvedValue(true);
    const impact = jest.spyOn(Haptics, 'impactAsync');
    await render(<Row onRemove={onRemove} />);
    const f = finger(rows[0]);
    await f.down();
    await dragTo(rows[0], -PAST);
    expect(impact).toHaveBeenCalledTimes(1);
    await f.release(-PAST);
    await willOpen(rows[0]);
    expect(onRemove).toHaveBeenCalledTimes(1);
    expect(rows[0].close).not.toHaveBeenCalled();
  });

  it('with Reduce Motion the row is already at the panel when told it opens: the release still removes', async () => {
    (useReducedMotion as jest.Mock).mockReturnValue(true);
    const onRemove = jest.fn().mockResolvedValue(true);
    await render(<Row onRemove={onRemove} />);
    await swipe(rows[0], -PAST);
    // The spring target is applied at once: the live value is the panel width.
    await act(async () => {
      rows[0].translation.value = -REMOVE_PANEL_WIDTH;
    });
    await willOpen(rows[0]);
    expect(onRemove).toHaveBeenCalledTimes(1);
  });

  it('a release under the distance does not remove, even when the live value is past it', async () => {
    const onRemove = jest.fn();
    await render(<Row onRemove={onRemove} />);
    await swipe(rows[0], -(FULL_SWIPE_DISTANCE - 10));
    await act(async () => {
      rows[0].translation.value = -PAST;
    });
    await willOpen(rows[0]);
    expect(onRemove).not.toHaveBeenCalled();
  });

  it('a fast flick released past the distance before the haptic reaction ran still removes', async () => {
    const onRemove = jest.fn().mockResolvedValue(true);
    const impact = jest.spyOn(Haptics, 'impactAsync');
    await render(<Row onRemove={onRemove} />);
    // The live translation never moved: only the release says how far it went.
    await swipe(rows[0], -PAST);
    await willOpen(rows[0]);
    expect(impact).not.toHaveBeenCalled();
    expect(onRemove).toHaveBeenCalledTimes(1);
  });

  it('removes whichever of the release and "will open" reaches JS first', async () => {
    const onRemove = jest.fn().mockResolvedValue(true);
    await render(<Row onRemove={onRemove} />);
    const f = finger(rows[0]);
    await f.down();
    await willOpen(rows[0]);
    expect(onRemove).not.toHaveBeenCalled();
    await f.release(-PAST);
    expect(onRemove).toHaveBeenCalledTimes(1);
  });

  it('counts the travel from an already open row', async () => {
    const onRemove = jest.fn().mockResolvedValue(true);
    await render(<Row onRemove={onRemove} />);
    await openAtRest(rows[0]);
    expect(onRemove).not.toHaveBeenCalled();
    // 88 open + 98 more = past 176.
    await swipe(rows[0], -(FULL_SWIPE_DISTANCE - REMOVE_PANEL_WIDTH + 10));
    await willOpen(rows[0]);
    expect(onRemove).toHaveBeenCalledTimes(1);
  });

  it('a grab while the row springs closed counts from 0, as the swipeable does', async () => {
    const onRemove = jest.fn();
    await render(<Row onRemove={onRemove} />);
    await openAtRest(rows[0]);
    // A tap closes it; the closing spring is at 60 when the finger lands again.
    await willClose(rows[0]);
    await rowAt(rows[0], -60);
    // The swipeable shows 0 + 120 = 120: under the distance.
    await swipe(rows[0], -120);
    await willOpen(rows[0]);
    expect(onRemove).not.toHaveBeenCalled();
  });

  it('a grab mid-opening-spring never adds the spring position (Codex)', async () => {
    const onRemove = jest.fn();
    await render(<Row onRemove={onRemove} />);
    // A short swipe let go at 150 opens the row: the spring heads for 88.
    await swipe(rows[0], -150);
    await willOpen(rows[0]);
    await rowAt(rows[0], -140);
    // Grabbed at 140 and dragged 50 more: the swipeable shows 88 + 50 = 138.
    await swipe(rows[0], -50);
    await willOpen(rows[0]);
    expect(onRemove).not.toHaveBeenCalled();
  });

  it('a row still drawn on the panel but already closing counts from 0', async () => {
    const onRemove = jest.fn();
    await render(<Row onRemove={onRemove} />);
    await openAtRest(rows[0]);
    // Closed (tap, another row opened, a failed removal), before the spring moved.
    await willClose(rows[0]);
    await swipe(rows[0], -(FULL_SWIPE_DISTANCE - REMOVE_PANEL_WIDTH + 10));
    await willOpen(rows[0]);
    expect(onRemove).not.toHaveBeenCalled();
  });

  it('a swipe cancelled while active past the distance does not remove', async () => {
    const onRemove = jest.fn();
    await render(<Row onRemove={onRemove} />);
    const f = finger(rows[0]);
    await f.down();
    await f.cancel(-PAST);
    await willOpen(rows[0]);
    expect(onRemove).not.toHaveBeenCalled();
  });

  it('a tap leaves nothing behind: a later full swipe still removes once', async () => {
    const onRemove = jest.fn().mockResolvedValue(true);
    await render(<Row onRemove={onRemove} />);
    await finger(rows[0]).tap();
    await willOpen(rows[0]);
    expect(onRemove).not.toHaveBeenCalled();
    await swipe(rows[0], -PAST);
    await willOpen(rows[0]);
    expect(onRemove).toHaveBeenCalledTimes(1);
  });

  it('a release past the distance that closes the row (a flick back) does not remove', async () => {
    const onRemove = jest.fn();
    await render(<Row onRemove={onRemove} />);
    await swipe(rows[0], -PAST);
    await act(async () => rows[0].props.onSwipeableWillClose?.('left'));
    await settle();
    await willOpen(rows[0]);
    expect(onRemove).not.toHaveBeenCalled();
  });

  it('a drag that crosses the distance and comes back under it before letting go does not remove', async () => {
    const onRemove = jest.fn().mockResolvedValue(true);
    await render(<Row onRemove={onRemove} />);
    const f = finger(rows[0]);
    await f.down();
    // Past the distance long enough for the haptic reaction to arm…
    await dragTo(rows[0], -PAST);
    // …then back under it and let go.
    await f.release(-REMOVE_PANEL_WIDTH);
    await willOpen(rows[0]);
    expect(onRemove).not.toHaveBeenCalled();
  });

  it('a short swipe only opens the row on the panel', async () => {
    const onRemove = jest.fn();
    await render(<Row onRemove={onRemove} />);
    await swipe(rows[0], -REMOVE_PANEL_WIDTH);
    await willOpen(rows[0]);
    expect(onRemove).not.toHaveBeenCalled();
  });

  it('a later "will open" with no swipe of its own does not reuse an old release', async () => {
    const onRemove = jest.fn();
    await render(<Row onRemove={onRemove} />);
    await swipe(rows[0], -PAST);
    // That swipe closed the row; the next touch starts afresh.
    await act(async () => rows[0].props.onSwipeableWillClose?.('left'));
    await finger(rows[0]).down();
    await willOpen(rows[0]);
    expect(onRemove).not.toHaveBeenCalled();
  });

  it('springs the row back when the removal fails', async () => {
    let finish: (removed: boolean) => void = () => {};
    const onRemove = jest.fn(() => new Promise<boolean>((resolve) => (finish = resolve)));
    await render(<Row onRemove={onRemove} />);
    await swipe(rows[0], -PAST);
    await willOpen(rows[0]);
    expect(rows[0].close).not.toHaveBeenCalled();
    await act(async () => finish(false));
    expect(rows[0].close).toHaveBeenCalledTimes(1);
  });

  it('springs the row back when the removal throws', async () => {
    const onRemove = jest.fn().mockRejectedValue(new Error('500'));
    await render(<Row onRemove={onRemove} />);
    await swipe(rows[0], -PAST);
    await willOpen(rows[0]);
    await waitFor(() => expect(rows[0].close).toHaveBeenCalledTimes(1));
  });

  it('disables the gesture, its observer and the panel while offline and closes an open row', async () => {
    const onRemove = jest.fn();
    const screen = await render(<Row onRemove={onRemove} />);
    expect(rows[0].props.enabled).toBe(true);
    await screen.rerender(<Row onRemove={onRemove} disabled />);
    expect(rows[0].props.enabled).toBe(false);
    expect(rows[0].close).toHaveBeenCalled();
    expect(
      screen.getByTestId('swipe-remove-panel', { includeHiddenElements: true })
    ).toBeDisabled();
    // The disabled observer records nothing.
    await swipe(rows[0], -PAST);
    await willOpen(rows[0]);
    expect(onRemove).not.toHaveBeenCalled();
  });

  it('does not close the row when it is disabled by its own removal in flight', async () => {
    let finish: (removed: boolean) => void = () => {};
    const onRemove = jest.fn(() => new Promise<boolean>((resolve) => (finish = resolve)));
    const screen = await render(<Row onRemove={onRemove} />);
    await swipe(rows[0], -PAST);
    await willOpen(rows[0]);
    // The screen marks the book as mutating, which disables the row.
    await screen.rerender(<Row onRemove={onRemove} disabled />);
    expect(rows[0].close).not.toHaveBeenCalled();
    await act(async () => finish(true));
    expect(rows[0].close).not.toHaveBeenCalled();
  });

  it('observes the swipe alongside it with the same 20px activation and no other condition', async () => {
    await render(<Row onRemove={jest.fn()} />);
    const observer = rows[0].props.simultaneousWith as {
      type: string;
      config: Record<string, unknown>;
    };
    expect(observer.type).toBe('PanGestureHandler');
    expect(observer.config).toMatchObject({
      activeOffsetXStart: -SWIPE_ACTIVATION_OFFSET,
      activeOffsetXEnd: SWIPE_ACTIVATION_OFFSET,
    });
    for (const key of ['failOffsetXStart', 'activeOffsetYStart', 'failOffsetYStart', 'minDist']) {
      expect(observer.config[key]).toBeUndefined();
    }
  });

  it('takes the touch only after 20px sideways, so vertical scrolling wins', async () => {
    await render(<Row onRemove={jest.fn()} />);
    expect(rows[0].props.dragOffsetFromLeft).toBe(SWIPE_ACTIVATION_OFFSET);
    expect(rows[0].props.dragOffsetFromRight).toBe(-SWIPE_ACTIVATION_OFFSET);
    expect(SWIPE_ACTIVATION_OFFSET).toBeGreaterThan(10);
  });

  it('puts the panel on the right in LTR and on the left in RTL, mirroring the full swipe', async () => {
    await render(<Row onRemove={jest.fn()} />);
    expect(rows[0].props.renderRightActions).toBeDefined();
    expect(rows[0].props.renderLeftActions).toBeUndefined();

    rows.length = 0;
    const restore = jest.replaceProperty(I18nManager, 'isRTL', true);
    try {
      const onRemove = jest.fn().mockResolvedValue(true);
      await render(<Row onRemove={onRemove} />);
      expect(rows[0].props.renderLeftActions).toBeDefined();
      expect(rows[0].props.renderRightActions).toBeUndefined();
      // A left swipe does nothing in RTL; a full right swipe removes.
      await swipe(rows[0], -PAST);
      await willOpen(rows[0], 'right');
      expect(onRemove).not.toHaveBeenCalled();
      await swipe(rows[0], PAST);
      await willOpen(rows[0], 'right');
      expect(onRemove).toHaveBeenCalledTimes(1);
    } finally {
      restore.restore();
    }
  });

  it('never overshoots its spring, and does not animate with Reduce Motion on', async () => {
    const screen = await render(<Row onRemove={jest.fn()} />);
    expect(rows[0].props.animationOptions).toEqual(SPRING_OPTIONS);
    expect(SPRING_OPTIONS.overshootClamping).toBe(true);

    (useReducedMotion as jest.Mock).mockReturnValue(true);
    await screen.rerender(<Row onRemove={jest.fn()} />);
    expect(rows[0].props.animationOptions).toEqual(REDUCED_MOTION_OPTIONS);
    expect(REDUCED_MOTION_OPTIONS).toEqual({ overshootClamping: true, reduceMotion: 'always' });
  });

  it('keeps only one row open: opening a row closes the other', async () => {
    function List() {
      const group = useSingleOpenRow();
      return (
        <>
          <Row onRemove={jest.fn()} group={group} title="Dune" />
          <Row onRemove={jest.fn()} group={group} title="Emma" />
        </>
      );
    }
    await render(<List />);
    const [dune, emma] = rows;
    await act(async () => dune.props.onSwipeableWillOpen?.('left'));
    expect(dune.close).not.toHaveBeenCalled();
    await act(async () => emma.props.onSwipeableWillOpen?.('left'));
    expect(dune.close).toHaveBeenCalledTimes(1);
    expect(emma.close).not.toHaveBeenCalled();
  });
});

describe('useSingleOpenRow', () => {
  it('closeOpen (list scroll) closes the open row once', async () => {
    const { result } = await renderHook(() => useSingleOpenRow());
    const row = { close: jest.fn() };
    result.current.onOpen(row);
    result.current.closeOpen();
    result.current.closeOpen();
    expect(row.close).toHaveBeenCalledTimes(1);
  });

  it('forgets a row that closed itself, and reopening the same row closes nothing', async () => {
    const { result } = await renderHook(() => useSingleOpenRow());
    const group = result.current;
    const a = { close: jest.fn() };
    const b = { close: jest.fn() };
    group.onOpen(a);
    group.onOpen(a);
    expect(a.close).not.toHaveBeenCalled();
    group.onClose(b); // not the open row: ignored
    group.onClose(a);
    group.onOpen(b);
    group.closeOpen();
    expect(a.close).not.toHaveBeenCalled();
    expect(b.close).toHaveBeenCalledTimes(1);
  });

  it('is stable across renders, so memoised rows do not re-render', async () => {
    const { result, rerender } = await renderHook(() => useSingleOpenRow());
    const first = result.current;
    await rerender({});
    expect(result.current).toBe(first);
  });
});

describe('SwipeToRemove.web (#935)', () => {
  it('renders only its children: no swipe, no panel', async () => {
    const screen = await render(
      <WebSwipeToRemove onRemove={jest.fn()} label="Remove Dune from wishlist" actionText="Remove">
        <Text>Dune</Text>
      </WebSwipeToRemove>
    );
    expect(screen.getByText('Dune')).toBeTruthy();
    expect(screen.queryByTestId('swipe-to-remove')).toBeNull();
    expect(screen.queryByTestId('swipe-remove-panel')).toBeNull();
    expect(screen.toJSON()).toMatchObject({ type: 'Text' });
    expect(rows).toHaveLength(0);
  });
});

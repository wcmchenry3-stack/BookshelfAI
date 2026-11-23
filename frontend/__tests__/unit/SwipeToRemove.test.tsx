/**
 * Swipe to remove (#935). ReanimatedSwipeable is replaced by a stand-in that
 * renders its actions with a real Reanimated shared value as the translation
 * and keeps its props, so a test can drive the swipe through the callbacks
 * gesture-handler would call (open, close) and move the translation past the
 * full-swipe distance. The gesture itself needs a device (owner check).
 */
import React from 'react';
import { I18nManager, Text } from 'react-native';
import { act, fireEvent, render, renderHook, waitFor } from '@testing-library/react-native';
import * as Haptics from 'expo-haptics';
import { useReducedMotion, type SharedValue } from 'react-native-reanimated';

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
    onSwipeableClose?: (direction: string) => void;
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
  onRemove: () => unknown;
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

beforeEach(() => {
  rows.length = 0;
  jest.clearAllMocks();
  (useReducedMotion as jest.Mock).mockReturnValue(false);
});

describe('SwipeToRemove (#935)', () => {
  it('reveals an 88px danger panel that is a button labelled "Remove {title} from wishlist"', async () => {
    const screen = await render(<Row onRemove={jest.fn()} />);
    const panel = screen.getByRole('button', { name: 'Remove Dune from wishlist' });
    expect(panel.props.testID).toBe('swipe-remove-panel');
    expect(REMOVE_PANEL_WIDTH).toBe(88);
    expect(panel).toHaveStyle({ width: 88, backgroundColor: '#B3261E' });
    expect(screen.getByText('Remove')).toBeTruthy();
    expect(screen.getByText('Dune')).toBeTruthy();
  });

  it('pressing the panel removes the book, then closes the row', async () => {
    const onRemove = jest.fn().mockResolvedValue(undefined);
    const screen = await render(<Row onRemove={onRemove} />);
    await act(async () => {
      fireEvent.press(screen.getByRole('button', { name: 'Remove Dune from wishlist' }));
    });
    expect(onRemove).toHaveBeenCalledTimes(1);
    expect(rows[0].close).toHaveBeenCalled();
  });

  it('a full swipe removes the book straight away, with a haptic tick at the threshold', async () => {
    const onRemove = jest.fn().mockResolvedValue(undefined);
    const impact = jest.spyOn(Haptics, 'impactAsync');
    await render(<Row onRemove={onRemove} />);
    await dragTo(rows[0], -(FULL_SWIPE_DISTANCE + 10));
    expect(impact).toHaveBeenCalledTimes(1);
    await act(async () => rows[0].props.onSwipeableWillOpen?.('left'));
    expect(onRemove).toHaveBeenCalledTimes(1);
  });

  it('a short swipe only opens the row on the panel', async () => {
    const onRemove = jest.fn();
    await render(<Row onRemove={onRemove} />);
    await dragTo(rows[0], -REMOVE_PANEL_WIDTH);
    await act(async () => rows[0].props.onSwipeableWillOpen?.('left'));
    expect(onRemove).not.toHaveBeenCalled();
  });

  it('dragging back under the full-swipe distance before letting go does not remove', async () => {
    const onRemove = jest.fn();
    await render(<Row onRemove={onRemove} />);
    await dragTo(rows[0], -(FULL_SWIPE_DISTANCE + 10));
    await dragTo(rows[0], -REMOVE_PANEL_WIDTH);
    await act(async () => rows[0].props.onSwipeableWillOpen?.('left'));
    expect(onRemove).not.toHaveBeenCalled();
  });

  it('springs the row back when the removal fails', async () => {
    let fail: (err: Error) => void = () => {};
    const onRemove = jest.fn(() => new Promise<void>((_resolve, reject) => (fail = reject)));
    await render(<Row onRemove={onRemove} />);
    await dragTo(rows[0], -(FULL_SWIPE_DISTANCE + 10));
    await act(async () => rows[0].props.onSwipeableWillOpen?.('left'));
    expect(rows[0].close).not.toHaveBeenCalled();
    await act(async () => fail(new Error('500')));
    await waitFor(() => expect(rows[0].close).toHaveBeenCalled());
  });

  it('disables the gesture while offline and closes an open row', async () => {
    const screen = await render(<Row onRemove={jest.fn()} />);
    expect(rows[0].props.enabled).toBe(true);
    await screen.rerender(<Row onRemove={jest.fn()} disabled />);
    expect(rows[0].props.enabled).toBe(false);
    expect(rows[0].close).toHaveBeenCalled();
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
      const onRemove = jest.fn().mockResolvedValue(undefined);
      await render(<Row onRemove={onRemove} />);
      expect(rows[0].props.renderLeftActions).toBeDefined();
      expect(rows[0].props.renderRightActions).toBeUndefined();
      // A left swipe does nothing in RTL; a full right swipe removes.
      await dragTo(rows[0], -(FULL_SWIPE_DISTANCE + 10));
      await act(async () => rows[0].props.onSwipeableWillOpen?.('right'));
      expect(onRemove).not.toHaveBeenCalled();
      await dragTo(rows[0], FULL_SWIPE_DISTANCE + 10);
      await act(async () => rows[0].props.onSwipeableWillOpen?.('right'));
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

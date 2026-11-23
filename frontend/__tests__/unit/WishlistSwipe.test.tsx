/**
 * Wishlist swipe to remove, at the screen (#935): the row's `remove`
 * screen-reader action, the footer hint, the offline switch, closing the open
 * row on scroll, and My Books keeping its ⋯ menu with no swipe. The swipe
 * itself is covered in SwipeToRemove.test.tsx; here `SwipeToRemove` renders for
 * real and its props are recorded.
 */
import React from 'react';
import { AccessibilityInfo, Alert, Platform } from 'react-native';
import { act, fireEvent, render, waitFor } from '@testing-library/react-native';

import WishlistScreen from '../../app/(tabs)/wishlist';
import MyBooksScreen from '../../app/(tabs)/my-books';
import type { SwipeToRemoveProps } from '../../components/library/swipeToRemoveShared';
import { userBookRow } from '../helpers/screenMocks';

const mockGet = jest.fn();
const mockDelete = jest.fn();
const mockSwipeProps = jest.fn<void, [SwipeToRemoveProps]>();
let mockConnected = true;

jest.mock('../../lib/api', () => ({
  api: {
    get: (...args: unknown[]) => mockGet(...args),
    patch: jest.fn().mockResolvedValue({ data: {} }),
    delete: (...args: unknown[]) => mockDelete(...args),
  },
}));
jest.mock('../../hooks/useNetworkStatus', () => ({
  useNetworkStatus: () => ({ isConnected: mockConnected }),
}));
jest.mock('../../hooks/useTheme', () => require('../helpers/screenMocks').themeModule());
jest.mock('../../components/LoadingSpinner', () =>
  require('../helpers/screenMocks').loadingSpinnerModule()
);
jest.mock('../../components/library/SwipeToRemove', () => {
  const React = jest.requireActual('react');
  const actual = jest.requireActual('../../components/library/SwipeToRemove');
  return {
    SwipeToRemove: (props: SwipeToRemoveProps) => {
      mockSwipeProps(props);
      return React.createElement(actual.SwipeToRemove, props);
    },
  };
});

const DUNE = userBookRow();
const EMMA = userBookRow({
  id: 'ub-2',
  book: { id: 'b-2', title: 'Emma', author: 'Jane Austen', cover_url: null },
});
const DUNE_ROW = 'Dune — Wishlist, by Frank Herbert';

let screenReaderListener: ((on: boolean) => void) | undefined;

beforeEach(() => {
  jest.clearAllMocks();
  mockConnected = true;
  mockGet.mockResolvedValue({ data: [DUNE, EMMA] });
  mockDelete.mockResolvedValue({});
  jest.spyOn(AccessibilityInfo, 'isScreenReaderEnabled').mockResolvedValue(false);
  jest.spyOn(AccessibilityInfo, 'addEventListener').mockImplementation(((
    _event: string,
    listener: (on: boolean) => void
  ) => {
    screenReaderListener = listener;
    return { remove: jest.fn() };
  }) as unknown as typeof AccessibilityInfo.addEventListener);
});

async function renderWishlist() {
  const screen = await render(<WishlistScreen />);
  await waitFor(() => screen.getByLabelText(DUNE_ROW));
  return screen;
}

/** The latest props `SwipeToRemove` got for the row titled `title`. */
function swipePropsFor(title: string): SwipeToRemoveProps {
  const calls = mockSwipeProps.mock.calls.filter(([p]) => p.label.includes(title));
  return calls[calls.length - 1][0];
}

describe('Wishlist swipe to remove (#935)', () => {
  it('wraps every row in SwipeToRemove with its labels', async () => {
    const screen = await renderWishlist();
    expect(screen.getAllByTestId('swipe-to-remove')).toHaveLength(2);
    expect(swipePropsFor('Dune')).toMatchObject({
      label: 'Remove Dune from wishlist',
      actionText: 'Remove',
      disabled: false,
    });
    expect(screen.getByRole('button', { name: 'Remove Emma from wishlist' })).toBeTruthy();
  });

  it('swiping fully removes through removeBook: DELETE, the book leaves the list', async () => {
    const screen = await renderWishlist();
    await act(async () => {
      await swipePropsFor('Dune').onRemove();
    });
    expect(mockDelete).toHaveBeenCalledWith('/user-books/ub-1');
    await waitFor(() => expect(screen.queryByLabelText(DUNE_ROW)).toBeNull());
  });

  it('the row body exposes a "remove" screen-reader action; activating the row still opens the sheet', async () => {
    const screen = await renderWishlist();
    const body = screen.getByLabelText(DUNE_ROW);
    expect(body.props.accessibilityActions).toEqual([{ name: 'remove', label: 'Remove' }]);

    await fireEvent.press(body);
    await waitFor(() => expect(screen.getByLabelText('Remove Dune')).toBeTruthy());
    expect(mockDelete).not.toHaveBeenCalled();
  });

  it('onAccessibilityAction("remove") removes the book', async () => {
    const screen = await renderWishlist();
    await act(async () => {
      fireEvent(screen.getByLabelText(DUNE_ROW), 'accessibilityAction', {
        nativeEvent: { actionName: 'remove' },
      });
    });
    expect(mockDelete).toHaveBeenCalledWith('/user-books/ub-1');
    await waitFor(() => expect(screen.queryByLabelText(DUNE_ROW)).toBeNull());
  });

  it('ignores other accessibility actions', async () => {
    const screen = await renderWishlist();
    await act(async () => {
      fireEvent(screen.getByLabelText(DUNE_ROW), 'accessibilityAction', {
        nativeEvent: { actionName: 'activate' },
      });
    });
    expect(mockDelete).not.toHaveBeenCalled();
  });

  it('a failed removal keeps the book and shows the one existing error alert', async () => {
    const alertSpy = jest.spyOn(Alert, 'alert').mockImplementation(() => {});
    mockDelete.mockRejectedValue(new Error('Network error'));
    const screen = await renderWishlist();
    await act(async () => {
      await swipePropsFor('Dune').onRemove();
    });
    expect(alertSpy).toHaveBeenCalledTimes(1);
    expect(alertSpy).toHaveBeenCalledWith('Error', 'Could not remove book.');
    expect(screen.getByLabelText(DUNE_ROW)).toBeTruthy();
  });

  it('turns the swipe off while offline', async () => {
    mockConnected = false;
    await renderWishlist();
    expect(swipePropsFor('Dune').disabled).toBe(true);
  });

  it('closes the open row when the list starts scrolling', async () => {
    const screen = await renderWishlist();
    const row = { close: jest.fn() };
    swipePropsFor('Dune').group?.onOpen(row);
    fireEvent(screen.getByTestId('wishlist-list'), 'scrollBeginDrag');
    expect(row.close).toHaveBeenCalledTimes(1);
  });

  it('gives every row the same one-open-row group across renders', async () => {
    const screen = await renderWishlist();
    const group = swipePropsFor('Dune').group;
    expect(group).toBeDefined();
    expect(swipePropsFor('Emma').group).toBe(group);
    await fireEvent.changeText(screen.getByLabelText('Search your wishlist'), 'Dun');
    expect(swipePropsFor('Dune').group).toBe(group);
  });

  describe('footer hint', () => {
    const HINT = 'Swipe left on a book to remove it';

    it('shows on native when no screen reader is running', async () => {
      const screen = await renderWishlist();
      expect(screen.getByTestId('wishlist-swipe-hint')).toHaveTextContent(HINT);
    });

    it('hides while a screen reader is running, and returns when it stops', async () => {
      (AccessibilityInfo.isScreenReaderEnabled as jest.Mock).mockResolvedValue(true);
      const screen = await renderWishlist();
      await waitFor(() => expect(screen.queryByTestId('wishlist-swipe-hint')).toBeNull());

      await act(async () => screenReaderListener?.(false));
      expect(screen.getByTestId('wishlist-swipe-hint')).toBeTruthy();
      await act(async () => screenReaderListener?.(true));
      expect(screen.queryByTestId('wishlist-swipe-hint')).toBeNull();
    });

    it('hides when the search matches no book', async () => {
      const screen = await renderWishlist();
      await fireEvent.changeText(screen.getByLabelText('Search your wishlist'), 'zzz');
      expect(screen.queryByTestId('wishlist-swipe-hint')).toBeNull();
    });

    describe('on web', () => {
      const original = Platform.OS;
      beforeEach(() => {
        Platform.OS = 'web';
      });
      afterEach(() => {
        Platform.OS = original;
      });

      it('shows no hint and no remove action (removal goes through the detail sheet)', async () => {
        const screen = await renderWishlist();
        expect(screen.queryByTestId('wishlist-swipe-hint')).toBeNull();
        expect(screen.getByLabelText(DUNE_ROW).props.accessibilityActions).toBeUndefined();
        expect(AccessibilityInfo.isScreenReaderEnabled).not.toHaveBeenCalled();
      });
    });
  });
});

describe('My Books rows (#935)', () => {
  it('keep the ⋯ menu and get no swipe or remove action', async () => {
    mockGet.mockResolvedValue({ data: [DUNE] });
    const screen = await render(<MyBooksScreen />);
    await waitFor(() => screen.getByLabelText(DUNE_ROW));
    expect(screen.getByTestId('book-more-ub-1')).toBeTruthy();
    expect(screen.queryByTestId('swipe-to-remove')).toBeNull();
    expect(screen.queryByTestId('swipe-remove-panel')).toBeNull();
    expect(screen.getByLabelText(DUNE_ROW).props.accessibilityActions).toBeUndefined();
    expect(mockSwipeProps).not.toHaveBeenCalled();
  });
});

// Integration test for the server-authoritative contract (#475): library
// mutations never run offline. Uses the REAL useOnlineOnly / useNetworkStatus /
// OfflineNotice and the REAL screens; only the API, NetInfo and the router are
// mocked. Asserts that offline the user sees why, buttons are locked, and no
// request is ever sent — and that it all recovers when the connection returns.
import React from 'react';
import { act, fireEvent, render, waitFor } from '@testing-library/react-native';

import MyBooksScreen from '../../app/(tabs)/my-books';
import WishlistScreen from '../../app/(tabs)/wishlist';

const mockGet = jest.fn();
const mockPatch = jest.fn();
const mockDelete = jest.fn();

jest.mock('../../lib/api', () => ({
  api: {
    get: (...args: unknown[]) => mockGet(...args),
    patch: (...args: unknown[]) => mockPatch(...args),
    delete: (...args: unknown[]) => mockDelete(...args),
  },
}));

jest.mock('expo-router', () => ({
  useFocusEffect: (cb: () => void) => {
    require('react').useEffect(cb, []);
  },
}));

// Controllable connectivity: the initial state, plus a handle to push changes.
let mockInitiallyConnected = false;
let mockEmit: (isConnected: boolean) => void = () => {};
jest.mock('@react-native-community/netinfo', () => ({
  __esModule: true,
  default: {
    addEventListener: (cb: (state: { isConnected: boolean }) => void) => {
      mockEmit = (isConnected) => cb({ isConnected });
      cb({ isConnected: mockInitiallyConnected });
      return () => {};
    },
    fetch: jest.fn(),
  },
}));

jest.mock('../../hooks/useTheme', () => ({
  useTheme: () => ({
    theme: {
      isDark: false,
      colors: {
        background: '#fff',
        surface: '#f5f5f5',
        border: '#ccc',
        text: '#000',
        textSecondary: '#888',
        primary: '#007AFF',
        errorContainer: '#ffdad6',
        onErrorContainer: '#93000a',
      },
      typography: { fontSizeBase: 16, fontSizeSM: 14, fontSizeXS: 12 },
    },
  }),
}));

jest.mock('../../components/LoadingSpinner', () => ({
  LoadingSpinner: () => null,
}));

const USER_BOOK = {
  id: 'ub-1',
  status: 'wishlisted',
  rating: null,
  notes: null,
  wishlisted_at: null,
  purchased_at: null,
  started_at: null,
  finished_at: null,
  book: { id: 'b-1', title: 'Dune', author: 'Frank Herbert', cover_url: null, description: null },
  edition: { publish_year: 1965, publisher: null, page_count: null },
};

const isDisabled = (el: { props: { accessibilityState?: { disabled?: boolean } } }) =>
  el.props.accessibilityState?.disabled === true;

beforeEach(() => {
  jest.clearAllMocks();
  mockInitiallyConnected = false;
  mockGet.mockResolvedValue({ data: [USER_BOOK] });
  mockPatch.mockResolvedValue({ data: { ...USER_BOOK, status: 'purchased' } });
  mockDelete.mockResolvedValue({});
});

describe('Wishlist — offline', () => {
  it('shows the offline notice and locks the mutation buttons', async () => {
    const { findByLabelText, getByTestId } = await render(<WishlistScreen />);
    const markPurchased = await findByLabelText('Mark Dune as purchased');
    const remove = await findByLabelText('Remove Dune from wishlist');

    expect(getByTestId('offline-notice')).toBeTruthy();
    expect(isDisabled(markPurchased)).toBe(true);
    expect(isDisabled(remove)).toBe(true);
  });

  it('sends no request when a locked button is pressed', async () => {
    const { findByLabelText, queryByText } = await render(<WishlistScreen />);
    const markPurchased = await findByLabelText('Mark Dune as purchased');
    const remove = await findByLabelText('Remove Dune from wishlist');

    await act(async () => {
      await fireEvent.press(markPurchased);
      await fireEvent.press(remove);
    });

    expect(mockPatch).not.toHaveBeenCalled();
    expect(mockDelete).not.toHaveBeenCalled();
    expect(queryByText('Dune')).toBeTruthy();
  });

  it('re-enables actions and hides the notice when the connection returns', async () => {
    const { findByLabelText, queryByTestId } = await render(<WishlistScreen />);
    await findByLabelText('Mark Dune as purchased');

    await act(async () => {
      mockEmit(true);
    });

    await waitFor(() => expect(queryByTestId('offline-notice')).toBeNull());
    expect(isDisabled(await findByLabelText('Mark Dune as purchased'))).toBe(false);

    await act(async () => {
      await fireEvent.press(await findByLabelText('Mark Dune as purchased'));
    });
    expect(mockPatch).toHaveBeenCalledWith('/user-books/ub-1', { status: 'purchased' });
  });

  it('locks the buttons again if the connection drops', async () => {
    mockInitiallyConnected = true;
    const { findByLabelText, queryByTestId } = await render(<WishlistScreen />);
    expect(isDisabled(await findByLabelText('Mark Dune as purchased'))).toBe(false);
    expect(queryByTestId('offline-notice')).toBeNull();

    await act(async () => {
      mockEmit(false);
    });

    await waitFor(async () =>
      expect(isDisabled(await findByLabelText('Mark Dune as purchased'))).toBe(true)
    );
    expect(queryByTestId('offline-notice')).toBeTruthy();
  });
});

describe('My Books — offline', () => {
  it('shows the offline notice and locks card and sheet mutation buttons', async () => {
    const { findByLabelText, getByLabelText, getByTestId } = await render(<MyBooksScreen />);
    const markAs = await findByLabelText('Mark as Purchased');
    const remove = await findByLabelText('Remove Dune');

    expect(getByTestId('offline-notice')).toBeTruthy();
    expect(isDisabled(markAs)).toBe(true);
    expect(isDisabled(remove)).toBe(true);

    // The detail sheet's buttons are locked too.
    await fireEvent.press(getByLabelText('Dune — wishlisted'));
    await waitFor(() => {
      const sheetButtons = getByLabelText('Mark as Purchased', { includeHiddenElements: false });
      expect(isDisabled(sheetButtons)).toBe(true);
    });
  });

  it('sends no request when a locked button is pressed', async () => {
    const { findByLabelText } = await render(<MyBooksScreen />);
    const markAs = await findByLabelText('Mark as Purchased');
    const remove = await findByLabelText('Remove Dune');

    await act(async () => {
      await fireEvent.press(markAs);
      await fireEvent.press(remove);
    });

    expect(mockPatch).not.toHaveBeenCalled();
    expect(mockDelete).not.toHaveBeenCalled();
  });

  it('allows the mutation once the connection returns', async () => {
    const { findByLabelText, queryByTestId } = await render(<MyBooksScreen />);
    await findByLabelText('Mark as Purchased');

    await act(async () => {
      mockEmit(true);
    });
    await waitFor(() => expect(queryByTestId('offline-notice')).toBeNull());

    await act(async () => {
      await fireEvent.press(await findByLabelText('Mark as Purchased'));
    });
    expect(mockPatch).toHaveBeenCalledWith('/user-books/ub-1', { status: 'purchased' });
  });
});

import React from 'react';
import { act, fireEvent, render, waitFor } from '@testing-library/react-native';

import MyBooksScreen from '../../app/(tabs)/my-books';
import SettingsScreen from '../../app/(tabs)/settings';
import WishlistScreen from '../../app/(tabs)/wishlist';
import { notifyLibraryChanged } from '../../lib/libraryChanged';
import en from '../../src/i18n/locales/en/my-books.json';
import * as trashNav from '../../lib/trash';

// #683: deleting a library entry says it moves to the Trash (restorable for
// 30 days), offers "View trash", and Settings links to the Trash screen.

const mockGet = jest.fn();
const mockDelete = jest.fn();
jest.mock('../../lib/api', () => ({
  api: {
    get: (...a: unknown[]) => mockGet(...a),
    patch: jest.fn(),
    delete: (...a: unknown[]) => mockDelete(...a),
  },
}));

const mockPush = jest.fn();
jest.mock('expo-router', () => ({
  useFocusEffect: (cb: () => void) => {
    require('react').useEffect(cb, []);
  },
  router: { push: (...a: unknown[]) => mockPush(...a), back: jest.fn(), canGoBack: () => true },
}));

jest.mock('../../lib/sentry', () => ({
  Sentry: { addBreadcrumb: jest.fn(), captureException: jest.fn() },
}));

const mockShowBanner = jest.fn();
jest.mock('../../hooks/useBanner', () => ({
  useBanner: () => ({ banner: null, showBanner: mockShowBanner, hideBanner: jest.fn() }),
}));

jest.mock('../../hooks/useAuth', () => ({ useAuth: () => ({ logout: jest.fn() }) }));
jest.mock('../../components/ThemeToggleButton', () => ({ ThemeToggleButton: () => null }));

jest.mock('../../hooks/useTheme', () => ({
  useTheme: () => ({
    theme: {
      colors: new Proxy({}, { get: () => '#123456' }),
      spacing: { md: 16 },
      typography: new Proxy({}, { get: () => 14 }),
    },
  }),
}));

const BOOK = {
  id: 'ub-1',
  status: 'wishlisted',
  book: { id: 'b-1', title: 'Dune', author: 'Frank Herbert', cover_url: null, description: null },
  edition: null,
};

beforeEach(() => {
  jest.clearAllMocks();
  mockGet.mockResolvedValue({ data: [BOOK] });
  mockDelete.mockResolvedValue({});
});

describe('delete copy (#683)', () => {
  it('uses the Trash wording and never calls the delete permanent', () => {
    expect(en.movedToTrash).toBe('Moved to Trash. You can restore it within 30 days.');
    expect(en.movedToTrash).not.toMatch(/permanent/i);
    expect(en.viewTrash).toBe('View trash');
  });

  it('My Books tells the user the entry is in the Trash and offers View trash', async () => {
    const { findByLabelText } = await render(<MyBooksScreen />);
    await act(async () => {
      await fireEvent.press(await findByLabelText('Remove Dune'));
    });
    await waitFor(() => expect(mockShowBanner).toHaveBeenCalled());
    const config = mockShowBanner.mock.calls[0][0];
    expect(config.message).toBe('Moved to Trash. You can restore it within 30 days.');
    expect(config.actions[0].label).toBe('View trash');
    config.actions[0].onPress();
    expect(mockPush).toHaveBeenCalledWith(trashNav.TRASH_ROUTE);
  });

  it('shows no Trash message when the delete fails', async () => {
    mockDelete.mockRejectedValue({ response: { status: 500 } });
    const { findByLabelText } = await render(<MyBooksScreen />);
    await act(async () => {
      await fireEvent.press(await findByLabelText('Remove Dune'));
    });
    expect(mockShowBanner).not.toHaveBeenCalled();
  });
});

describe('Wishlist delete (#683)', () => {
  it('tells the user the entry is in the Trash and offers View trash', async () => {
    const { findByLabelText } = await render(<WishlistScreen />);
    await act(async () => {
      await fireEvent.press(await findByLabelText('Remove Dune from wishlist'));
    });
    await waitFor(() => expect(mockShowBanner).toHaveBeenCalled());
    const config = mockShowBanner.mock.calls[0][0];
    expect(config.message).toBe('Moved to Trash. You can restore it within 30 days.');
    config.actions[0].onPress();
    expect(mockPush).toHaveBeenCalledWith('/trash');
  });
});

describe('library-changed signal (#683)', () => {
  it('makes My Books reload', async () => {
    await render(<MyBooksScreen />);
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1));
    await act(async () => notifyLibraryChanged());
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
  });

  it('makes Wishlist reload', async () => {
    await render(<WishlistScreen />);
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1));
    await act(async () => notifyLibraryChanged());
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
  });

  it('stops reloading once the screen is gone', async () => {
    const { unmount } = await render(<MyBooksScreen />);
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1));
    await unmount();
    notifyLibraryChanged();
    expect(mockGet).toHaveBeenCalledTimes(1);
  });
});

describe('Settings Trash row (#683)', () => {
  it('opens the Trash screen', async () => {
    const { getByTestId, getByLabelText } = await render(<SettingsScreen />);
    expect(getByLabelText('Open the trash')).toBeTruthy();
    await fireEvent.press(getByTestId('settings-trash'));
    expect(mockPush).toHaveBeenCalledWith('/trash');
  });
});

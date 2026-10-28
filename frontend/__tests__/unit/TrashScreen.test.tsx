import React from 'react';
import { act, fireEvent, render, waitFor } from '@testing-library/react-native';

import TrashScreen from '../../app/trash';
import { onLibraryChanged } from '../../lib/libraryChanged';

// #683: the Trash screen (list, restore, errors, empty / loading / error).

jest.mock('../../lib/sentry', () => ({
  Sentry: { addBreadcrumb: jest.fn(), captureException: jest.fn() },
}));

const mockListTrash = jest.fn();
const mockRestore = jest.fn();
jest.mock('../../lib/api', () => ({
  listTrash: (...a: unknown[]) => mockListTrash(...a),
  restoreUserBook: (...a: unknown[]) => mockRestore(...a),
}));

const mockShowBanner = jest.fn();
jest.mock('../../hooks/useBanner', () => ({
  useBanner: () => ({ banner: null, showBanner: mockShowBanner, hideBanner: jest.fn() }),
}));

let mockConnected = true;
jest.mock('../../hooks/useNetworkStatus', () => ({
  useNetworkStatus: () => ({ isConnected: mockConnected }),
}));

const mockBack = jest.fn();
jest.mock('expo-router', () => ({
  useFocusEffect: (cb: () => void) => {
    require('react').useEffect(cb, []);
  },
  router: { push: jest.fn(), back: () => mockBack(), canGoBack: () => true, replace: jest.fn() },
}));

jest.mock('../../hooks/useTheme', () => ({
  useTheme: () => ({
    theme: {
      colors: {
        background: '#fff',
        border: '#ccc',
        text: '#000',
        textSecondary: '#888',
        primary: '#0f426f',
        onPrimary: '#ffffff',
        surfaceContainerLow: '#f5f3ef',
        tertiaryFixed: '#eee',
        onTertiaryFixed: '#111',
      },
      typography: {
        fontSizeBase: 16,
        fontSizeSM: 14,
        fontSizeLG: 20,
        fontSizeXS: 12,
        fontSizeH1: 32,
        fontFamilyHeadline: 'serif',
      },
    },
  }),
}));

jest.mock('../../components/LoadingSpinner', () => ({
  LoadingSpinner: ({ message }: { message?: string }) => {
    const { Text } = require('react-native');
    return <Text testID="loading-spinner">{message ?? 'Loading'}</Text>;
  },
}));

function entry(id: string, title: string, extra: Record<string, unknown> = {}) {
  return {
    id,
    status: 'read',
    rating: null,
    notes: null,
    book: { id: `b-${id}`, title, author: 'Frank Herbert', cover_url: null, description: null },
    deleted_at: '2026-10-01T12:00:00Z',
    purge_after: '2026-10-31T12:00:00Z',
    ...extra,
  };
}

const DUNE = entry('ub-1', 'Dune');
const FOUNDATION = entry('ub-2', 'Foundation', { unverified: true });

function apiError(status: number, detail?: string) {
  return { response: { status, data: detail ? { detail } : {} } };
}

beforeEach(() => {
  jest.clearAllMocks();
  mockConnected = true;
  mockListTrash.mockResolvedValue([DUNE, FOUNDATION]);
  mockRestore.mockResolvedValue({});
});

describe('TrashScreen', () => {
  it('shows the loading state first', async () => {
    mockListTrash.mockReturnValue(new Promise(() => {}));
    const { getByTestId, getByText } = await render(<TrashScreen />);
    expect(getByTestId('loading-spinner')).toBeTruthy();
    expect(getByText('Loading trash…')).toBeTruthy();
  });

  it('renders the entries with author, dates and the 30-day rule', async () => {
    const { findByText, getByText, getAllByText, getByLabelText, getByTestId } = await render(
      <TrashScreen />
    );
    await findByText('Dune');
    expect(getByText('Foundation')).toBeTruthy();
    expect(getAllByText('Frank Herbert')).toHaveLength(2);
    expect(getAllByText(/^Deleted .*2026.* · permanently removed .*2026/)).toHaveLength(2);
    expect(getByText(/kept for 30 days/)).toBeTruthy();
    expect(getByLabelText('Restore Dune').props.accessibilityRole).toBe('button');
    // Only the user's own (unverified) entry carries the badge.
    expect(getByTestId('trash-item-ub-2')).toBeTruthy();
    expect(getAllByText('Unverified')).toHaveLength(1);
  });

  it('shows the empty state', async () => {
    mockListTrash.mockResolvedValue([]);
    const { findByText, getByText } = await render(<TrashScreen />);
    await findByText('Trash is empty');
    expect(getByText(/appear here for 30 days/)).toBeTruthy();
  });

  it('shows the error state and retries', async () => {
    mockListTrash.mockRejectedValueOnce(apiError(500));
    const { findByTestId, getByText, getByLabelText, findByText } = await render(<TrashScreen />);
    await findByTestId('trash-error');
    expect(getByText('Could not load the trash.')).toBeTruthy();
    await fireEvent.press(getByLabelText('Try again'));
    await findByText('Dune');
    expect(mockListTrash).toHaveBeenCalledTimes(2);
  });

  it('uses the service-unavailable message for a 403 forbidden load', async () => {
    mockListTrash.mockRejectedValueOnce(apiError(403, 'forbidden'));
    const { findByText } = await render(<TrashScreen />);
    await findByText("Can't reach BookshelfAI right now. Please try again later.");
  });

  it('a successful restore removes the row and confirms', async () => {
    const { findByText, getByLabelText, queryByText } = await render(<TrashScreen />);
    await findByText('Dune');
    await act(async () => {
      await fireEvent.press(getByLabelText('Restore Dune'));
    });
    expect(mockRestore).toHaveBeenCalledWith('ub-1');
    await waitFor(() => expect(queryByText('Dune')).toBeNull());
    expect(queryByText('Foundation')).toBeTruthy();
    expect(mockShowBanner).toHaveBeenCalledWith(
      expect.objectContaining({
        message: 'Restored Dune to your library.',
        type: 'success',
      })
    );
    // The library tabs reload on focus, so it is current when the user goes back.
    expect(mockListTrash).toHaveBeenCalledTimes(1);
  });

  it('blocks a second press while a restore is in flight', async () => {
    let resolve!: () => void;
    mockRestore.mockReturnValue(new Promise<void>((r) => (resolve = r)));
    const { findByText, getByLabelText } = await render(<TrashScreen />);
    await findByText('Dune');
    await act(async () => {
      void fireEvent.press(getByLabelText('Restore Dune'));
      void fireEvent.press(getByLabelText('Restore Dune'));
    });
    expect(mockRestore).toHaveBeenCalledTimes(1);
    await act(async () => resolve());
  });

  it('a restore conflict shows its message and keeps the row', async () => {
    mockRestore.mockRejectedValue(apiError(409, 'restore_conflict'));
    const { findByText, getByLabelText, getByText } = await render(<TrashScreen />);
    await findByText('Dune');
    await act(async () => {
      await fireEvent.press(getByLabelText('Restore Dune'));
    });
    expect(mockShowBanner).toHaveBeenCalledWith({
      message: 'This book is already in your library.',
      type: 'error',
    });
    expect(getByText('Dune')).toBeTruthy();
    expect(mockShowBanner).not.toHaveBeenCalledWith(expect.objectContaining({ type: 'success' }));
  });

  it('the unverified limit shows the existing limit message', async () => {
    mockRestore.mockRejectedValue(apiError(409, 'unverified_limit_reached'));
    const { findByText, getByLabelText } = await render(<TrashScreen />);
    await findByText('Foundation');
    await act(async () => {
      await fireEvent.press(getByLabelText('Restore Foundation'));
    });
    expect(mockShowBanner).toHaveBeenCalledWith({
      message: "You've reached the limit of books you can add yourself.",
      type: 'error',
    });
  });

  it('a 404 refreshes the list', async () => {
    mockRestore.mockRejectedValue(apiError(404, 'Not found'));
    const { findByText, getByLabelText, queryByText } = await render(<TrashScreen />);
    await findByText('Dune');
    mockListTrash.mockResolvedValue([FOUNDATION]);
    await act(async () => {
      await fireEvent.press(getByLabelText('Restore Dune'));
    });
    await waitFor(() => expect(mockListTrash).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(queryByText('Dune')).toBeNull());
    expect(mockShowBanner).toHaveBeenCalledWith({
      message: 'This book is no longer in the trash.',
      type: 'error',
    });
  });

  it('a 5xx shows the generic error', async () => {
    mockRestore.mockRejectedValue(apiError(500));
    const { findByText, getByLabelText } = await render(<TrashScreen />);
    await findByText('Dune');
    await act(async () => {
      await fireEvent.press(getByLabelText('Restore Dune'));
    });
    expect(mockShowBanner).toHaveBeenCalledWith({
      message: "Couldn't restore this book. Try again.",
      type: 'error',
    });
  });

  it('disables Restore while offline', async () => {
    mockConnected = false;
    const { findByText, getByLabelText } = await render(<TrashScreen />);
    await findByText('Dune');
    expect(getByLabelText('Restore Dune').props.accessibilityState.disabled).toBe(true);
    await act(async () => {
      await fireEvent.press(getByLabelText('Restore Dune'));
    });
    expect(mockRestore).not.toHaveBeenCalled();
  });

  it('Back leaves the screen', async () => {
    const { findByText, getByLabelText } = await render(<TrashScreen />);
    await findByText('Dune');
    await fireEvent.press(getByLabelText('Back'));
    expect(mockBack).toHaveBeenCalled();
  });

  it('fires the library-changed signal after a successful restore only', async () => {
    const changed = jest.fn();
    const off = onLibraryChanged(changed);
    mockRestore.mockRejectedValueOnce(apiError(500));
    const { findByText, getByLabelText } = await render(<TrashScreen />);
    await findByText('Dune');
    await act(async () => {
      await fireEvent.press(getByLabelText('Restore Dune'));
    });
    expect(changed).not.toHaveBeenCalled();
    await act(async () => {
      await fireEvent.press(getByLabelText('Restore Dune'));
    });
    expect(changed).toHaveBeenCalledTimes(1);
    off();
  });

  it('can retry after a failed restore', async () => {
    mockRestore.mockRejectedValueOnce(apiError(500)).mockResolvedValueOnce({});
    const { findByText, getByLabelText, queryByText } = await render(<TrashScreen />);
    await findByText('Dune');
    await act(async () => {
      await fireEvent.press(getByLabelText('Restore Dune'));
    });
    expect(getByLabelText('Restore Dune').props.accessibilityState.disabled).toBe(false);
    await act(async () => {
      await fireEvent.press(getByLabelText('Restore Dune'));
    });
    expect(mockRestore).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(queryByText('Dune')).toBeNull());
  });

  it('a refresh that was in flight during a restore does not bring the row back', async () => {
    const { findByText, getByLabelText, queryByText, getByTestId } = await render(<TrashScreen />);
    await findByText('Dune');
    let resolveStale!: (rows: unknown[]) => void;
    mockListTrash.mockReturnValueOnce(new Promise((r) => (resolveStale = r)));
    await act(async () => {
      getByTestId('trash-list').props.refreshControl.props.onRefresh();
    });
    await act(async () => {
      await fireEvent.press(getByLabelText('Restore Dune'));
    });
    await waitFor(() => expect(queryByText('Dune')).toBeNull());
    await act(async () => resolveStale([DUNE, FOUNDATION]));
    expect(queryByText('Dune')).toBeNull();
    expect(queryByText('Foundation')).toBeTruthy();
  });

  it('shows an error banner when a refresh fails while rows are shown', async () => {
    const { findByText, getByTestId, getByText } = await render(<TrashScreen />);
    await findByText('Dune');
    mockListTrash.mockRejectedValueOnce(apiError(500));
    await act(async () => {
      getByTestId('trash-list').props.refreshControl.props.onRefresh();
    });
    await waitFor(() =>
      expect(mockShowBanner).toHaveBeenCalledWith({
        message: "Couldn't refresh the trash.",
        type: 'error',
      })
    );
    expect(getByText('Dune')).toBeTruthy();
  });
});

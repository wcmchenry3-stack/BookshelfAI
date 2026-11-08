import React from 'react';
import { act, fireEvent, render, waitFor } from '@testing-library/react-native';

import TrashScreen from '../../app/trash';
import { onLibraryChanged } from '../../lib/libraryChanged';
import { StyleSheet } from 'react-native';
import { lightTheme } from '../../theme/light';
import { fontFor } from '../../theme/typography';
import { hasTopPadding, TEST_INSET_TOP } from '../helpers/insets';

// #683: the Trash screen (list, restore, errors, empty / loading / error).

jest.mock('react-native-safe-area-context', () => require('../helpers/insets').safeAreaModule());
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

jest.mock('../../components/LoadingSpinner', () => ({
  LoadingSpinner: ({ message }: { message?: string }) => {
    const { Text } = require('react-native');
    return <Text testID="loading-spinner">{message ?? 'Loading'}</Text>;
  },
}));

// #902: the Trash cards draw their cover with BookCover (decorative: the card names the book).
const mockCovers: Record<string, unknown>[] = [];
jest.mock('../../components/ui/BookCover', () => {
  const actual = jest.requireActual('../../components/ui/BookCover');
  return {
    ...actual,
    BookCover: (props: Record<string, unknown>) => {
      mockCovers.push(props);
      return require('react').createElement(actual.BookCover, props);
    },
  };
});

// The days-left line counts calendar days from today, so the fixtures are dated
// relative to now (noon local, clear of any day edge).
function purgeIn(days: number): string {
  const n = new Date();
  return new Date(n.getFullYear(), n.getMonth(), n.getDate() + days, 12).toISOString();
}

function entry(id: string, title: string, extra: Record<string, unknown> = {}) {
  return {
    id,
    status: 'read',
    rating: null,
    notes: null,
    book: { id: `b-${id}`, title, author: 'Frank Herbert', cover_url: null, description: null },
    deleted_at: '2026-10-01T12:00:00Z',
    purge_after: purgeIn(21),
    ...extra,
  };
}

const DUNE = entry('ub-1', 'Dune');
const FOUNDATION = entry('ub-2', 'Foundation', { unverified: true, purge_after: purgeIn(2) });

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

  it('renders the entries with author, days left and the 30-day rule', async () => {
    const { findByText, getByText, getAllByText, getByLabelText, getByTestId } = await render(
      <TrashScreen />
    );
    await findByText('Dune');
    expect(getByText('Foundation')).toBeTruthy();
    expect(getAllByText('Frank Herbert')).toHaveLength(2);
    expect(getByText('Deleted for good in 21 days')).toBeTruthy();
    expect(getByText('Deleted for good in 2 days')).toBeTruthy();
    expect(getByText(/stay here for 30 days/)).toBeTruthy();
    expect(getByLabelText('Restore Dune').props.accessibilityRole).toBe('button');
    // Only the user's own (unverified) entry carries the badge.
    expect(getByTestId('trash-item-ub-2')).toBeTruthy();
    expect(getAllByText('Unverified')).toHaveLength(1);
  });

  it('draws the days-left line in danger at 3 days or fewer, and not at 4', async () => {
    mockListTrash.mockResolvedValue([
      entry('ub-3', 'Three', { purge_after: purgeIn(3) }),
      entry('ub-4', 'Four', { purge_after: purgeIn(4) }),
      entry('ub-0', 'Today', { purge_after: purgeIn(0) }),
      entry('ub-1d', 'Tomorrow', { purge_after: purgeIn(1) }),
    ]);
    const { findByText, getByText } = await render(<TrashScreen />);
    await findByText('Three');
    const colourOf = (text: string) => StyleSheet.flatten(getByText(text).props.style).color;
    const danger = lightTheme.colors.danger;
    expect(colourOf('Deleted for good in 3 days')).toBe(danger);
    expect(colourOf('Deleted for good in 4 days')).not.toBe(danger);
    expect(colourOf('Deleted for good today')).toBe(danger);
    expect(colourOf('Deleted for good in 1 day')).toBe(danger);
    // Colour is never the only signal: the number is in the text.
    expect(StyleSheet.flatten(getByText('Deleted for good in 3 days').props.style).fontFamily).toBe(
      fontFor('600')
    );
  });

  it('names title, author and days left in one row label', async () => {
    const { findByLabelText } = await render(<TrashScreen />);
    expect(await findByLabelText('Dune, Frank Herbert. Deleted for good in 21 days')).toBeTruthy();
  });

  it('puts Unverified in the label of an unverified row', async () => {
    const { findByLabelText } = await render(<TrashScreen />);
    // FOUNDATION is the unverified entry: the grouped label swallows the badge, so it says so.
    expect(
      await findByLabelText('Foundation, Frank Herbert. Deleted for good in 2 days. Unverified')
    ).toBeTruthy();
  });

  it('shows no status line and no trailing stop when purge_after is unreadable', async () => {
    mockListTrash.mockResolvedValue([
      entry('ub-9', 'Broken', { purge_after: 'nope' }),
      entry('ub-8', 'BrokenToo', { purge_after: 'nope', unverified: true }),
    ]);
    const { findByLabelText, queryByText, queryAllByTestId } = await render(<TrashScreen />);
    expect(await findByLabelText('Broken, Frank Herbert')).toBeTruthy();
    expect(await findByLabelText('BrokenToo, Frank Herbert. Unverified')).toBeTruthy();
    expect(queryByText('Read')).toBeNull();
    expect(queryAllByTestId('book-row-dot')).toHaveLength(0);
  });

  it('shows the compact top row: Back, a centred Trash heading and no serif title', async () => {
    const { findByText, getByRole, getByText } = await render(<TrashScreen />);
    await findByText('Dune');
    const heading = getByRole('header', { name: 'Trash' });
    const style = StyleSheet.flatten(heading.props.style);
    expect(style).toMatchObject({ fontSize: 17, textAlign: 'center' });
    expect(style.fontFamily).toBe(fontFor('600'));
    expect(style.fontWeight).toBeUndefined();
    expect(getByText('Trash')).toBe(heading);
    expect(getByRole('button', { name: 'Back' })).toBeTruthy();
  });

  it('draws each cover with a decorative xs BookCover (#902)', async () => {
    mockCovers.length = 0;
    const { findByText } = await render(<TrashScreen />);
    await findByText('Dune');
    expect(mockCovers.map((c) => c.title)).toEqual(expect.arrayContaining(['Dune', 'Foundation']));
    expect(mockCovers[0]).toMatchObject({ size: 'xs', decorative: true });
  });

  it('pads the loading state by the top safe-area inset (#936)', async () => {
    mockListTrash.mockReturnValue(new Promise(() => {}));
    const { getByTestId } = await render(<TrashScreen />);
    expect(hasTopPadding(getByTestId('loading-spinner'), TEST_INSET_TOP)).toBe(true);
  });

  it('pads the error state by the top safe-area inset (#936)', async () => {
    mockListTrash.mockRejectedValueOnce(apiError(500));
    const { findByTestId } = await render(<TrashScreen />);
    expect(hasTopPadding(await findByTestId('trash-error'), TEST_INSET_TOP)).toBe(true);
  });

  it('pads the list by the top safe-area inset (#936)', async () => {
    const { findByText } = await render(<TrashScreen />);
    expect(hasTopPadding(await findByText('Dune'), TEST_INSET_TOP)).toBe(true);
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

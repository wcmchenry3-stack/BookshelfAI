import React from 'react';
import { Alert } from 'react-native';
import { act, fireEvent, render, waitFor, within } from '@testing-library/react-native';

import WishlistScreen from '../../app/(tabs)/wishlist';
import { hasTopPadding, TEST_INSET_TOP } from '../helpers/insets';
import { routerState } from '../helpers/routerMock';

const mockGet = jest.fn();
const mockPatch = jest.fn();
const mockDelete = jest.fn();

jest.mock('react-native-safe-area-context', () => require('../helpers/insets').safeAreaModule());
jest.mock('../../lib/api', () => ({
  api: {
    get: (...args: unknown[]) => mockGet(...args),
    patch: (...args: unknown[]) => mockPatch(...args),
    delete: (...args: unknown[]) => mockDelete(...args),
  },
}));

jest.mock('../../hooks/useTheme', () => require('../helpers/screenMocks').themeModule());

jest.mock('../../components/LoadingSpinner', () => ({
  LoadingSpinner: ({ message }: { message?: string }) => {
    const { Text } = require('react-native');
    return <Text testID="loading-spinner">{message ?? 'Loading'}</Text>;
  },
}));

const BOOK_1 = {
  id: 'ub-1',
  status: 'wishlisted',
  book: { id: 'b-1', title: 'Dune', author: 'Frank Herbert', cover_url: null },
  edition: { publish_year: 1965 },
};
const BOOK_2 = {
  id: 'ub-2',
  status: 'wishlisted',
  book: { id: 'b-2', title: 'Foundation', author: 'Isaac Asimov', cover_url: null },
  edition: null,
};

beforeEach(() => {
  jest.clearAllMocks();
  mockGet.mockResolvedValue({ data: [] });
  mockPatch.mockResolvedValue({ data: {} });
  mockDelete.mockResolvedValue({});
});

const HIDDEN = { hidden: true };
/** Text inside the rows' text blocks (the generated cover also typesets the title). */
function rowTexts(screen: { queryAllByTestId: (id: string, o: object) => unknown[] }) {
  return (screen.queryAllByTestId('book-row-text', HIDDEN) as Parameters<typeof within>[0][]).map(
    (el) => within(el)
  );
}

describe('WishlistScreen', () => {
  it('shows loading spinner on mount', async () => {
    mockGet.mockReturnValue(new Promise(() => {})); // never resolves
    const { getByTestId } = await render(<WishlistScreen />);
    expect(getByTestId('loading-spinner')).toBeTruthy();
  });

  it('shows the designed empty state when the wishlist is empty', async () => {
    mockGet.mockResolvedValue({ data: [] });
    const { getByText, getByRole, queryByTestId } = await render(<WishlistScreen />);
    await waitFor(() => expect(getByText('Nothing on your wishlist yet')).toBeTruthy());
    expect(getByRole('header', { name: 'Wishlist' })).toBeTruthy();
    expect(getByText('Books you want to buy')).toBeTruthy();
    expect(getByText(/Scan a bookshop shelf or search by title/)).toBeTruthy();
    // No search field when there is nothing to search.
    expect(queryByTestId('wishlist-search')).toBeNull();
  });

  it('hides the empty-state illustration from screen readers', async () => {
    const { getByTestId } = await render(<WishlistScreen />);
    await waitFor(() => expect(getByTestId('wishlist-empty-illustration', HIDDEN)).toBeTruthy());
    const props = getByTestId('wishlist-empty-illustration', HIDDEN).props;
    expect(props.accessibilityElementsHidden).toBe(true);
    expect(props.importantForAccessibility).toBe('no-hide-descendants');
    expect(props['aria-hidden']).toBe(true);
  });

  it('"Scan books" goes to Scan and "Search by title" to Scan in text mode', async () => {
    const { getByText } = await render(<WishlistScreen />);
    await waitFor(() => expect(getByText('Scan books')).toBeTruthy());
    await fireEvent.press(getByText('Scan books'));
    expect(routerState.push).toHaveBeenLastCalledWith('/scan');
    await fireEvent.press(getByText('Search by title'));
    expect(routerState.push).toHaveBeenLastCalledWith('/scan?mode=text');
  });

  it('pads the header by the top safe-area inset (#936), also when empty', async () => {
    mockGet.mockResolvedValue({ data: [] });
    const { getByText } = await render(<WishlistScreen />);
    await waitFor(() => expect(getByText('Nothing on your wishlist yet')).toBeTruthy());
    expect(hasTopPadding(getByText('Wishlist'), TEST_INSET_TOP)).toBe(true);
  });

  it.each([
    [1, '1 book you want to buy'],
    [2, '2 books you want to buy'],
  ])('subtitle with %i book(s) reads "%s"', async (count, text) => {
    mockGet.mockResolvedValue({ data: [BOOK_1, BOOK_2].slice(0, count) });
    const { getByTestId } = await render(<WishlistScreen />);
    await waitFor(() => expect(getByTestId('wishlist-subtitle').props.children).toBe(text));
  });

  it('filters by title or author, shows a no-match state and clears it', async () => {
    mockGet.mockResolvedValue({ data: [BOOK_1, BOOK_2] });
    const screen = await render(<WishlistScreen />);
    const { getByTestId, getByText, getByRole, getByLabelText } = screen;
    await waitFor(() => expect(getByTestId('wishlist-search')).toBeTruthy());
    const input = getByLabelText('Search your wishlist');
    const titles = () => rowTexts(screen).map((r) => r.queryByText(/Dune|Foundation/, HIDDEN));

    await fireEvent.changeText(input, 'asimov'); // the author, in any case
    await waitFor(() => expect(titles()).toHaveLength(1));
    expect(rowTexts(screen)[0].getByText('Foundation', HIDDEN)).toBeTruthy();
    // The subtitle counts the whole wishlist, not the matches.
    expect(getByTestId('wishlist-subtitle').props.children).toBe('2 books you want to buy');

    await fireEvent.changeText(input, 'zzz');
    await waitFor(() => expect(getByText('No books match “zzz”')).toBeTruthy());
    // The header and the search field stay.
    expect(getByRole('header', { name: 'Wishlist' })).toBeTruthy();
    expect(getByTestId('wishlist-search')).toBeTruthy();

    await fireEvent.press(getByText('Clear search'));
    await waitFor(() => expect(titles()).toHaveLength(2));
  });

  it('renders book titles and authors when data loaded', async () => {
    mockGet.mockResolvedValue({ data: [BOOK_1, BOOK_2] });
    const screen = await render(<WishlistScreen />);
    await waitFor(
      () => {
        const rows = rowTexts(screen);
        expect(rows).toHaveLength(2);
        expect(rows[0].getByText('Dune', HIDDEN)).toBeTruthy();
        expect(rows[0].getByText('Frank Herbert', HIDDEN)).toBeTruthy();
        expect(rows[1].getByText('Foundation', HIDDEN)).toBeTruthy();
      },
      { timeout: 10_000 }
    );
  }, 15_000);

  it('renders only the publish year in the meta line (no status on the wishlist)', async () => {
    mockGet.mockResolvedValue({ data: [BOOK_1] });
    const { getByText, queryByTestId } = await render(<WishlistScreen />);
    await waitFor(() => expect(getByText('1965', HIDDEN)).toBeTruthy());
    expect(queryByTestId('book-row-dot', HIDDEN)).toBeNull();
  });

  it('calls PATCH and removes book when Bought pressed', async () => {
    mockGet.mockResolvedValue({ data: [BOOK_1] });
    const screen = await render(<WishlistScreen />);
    const { getByLabelText } = screen;
    const queryByText = (text: string) => rowTexts(screen)[0]?.queryByText(text, HIDDEN) ?? null;
    await waitFor(() => expect(getByLabelText('Mark Dune as bought')).toBeTruthy());

    await act(async () => {
      await fireEvent.press(getByLabelText('Mark Dune as bought'));
    });

    expect(mockPatch).toHaveBeenCalledWith('/user-books/ub-1', { status: 'purchased' });
    await waitFor(() => expect(queryByText('Dune')).toBeNull());
  });

  it('keeps the book and locks its buttons until PATCH resolves, then removes it', async () => {
    mockGet.mockResolvedValue({ data: [BOOK_1] });
    const screen = await render(<WishlistScreen />);
    const { getByLabelText } = screen;
    const queryByText = (text: string) => rowTexts(screen)[0]?.queryByText(text, HIDDEN) ?? null;

    let resolvePatch!: () => void;
    mockPatch.mockReturnValue(
      new Promise((resolve) => {
        resolvePatch = () => resolve({ data: {} });
      })
    );

    await waitFor(() => expect(getByLabelText('Mark Dune as bought')).toBeTruthy());

    // Deliberately not awaited: under v14, awaiting fireEvent.press blocks
    // until all work the event triggers has settled, which would hang here
    // on the still-pending PATCH call.
    fireEvent.press(getByLabelText('Mark Dune as bought'));

    // Server-authoritative: no optimistic removal while the request is in flight.
    await waitFor(() =>
      expect(getByLabelText('Mark Dune as bought').props.accessibilityState.disabled).toBe(true)
    );
    expect(queryByText('Dune')).toBeTruthy();

    await act(async () => {
      resolvePatch();
    });
    await waitFor(() => expect(queryByText('Dune')).toBeNull());
  });

  it('ignores a second press while a request for the same book is in flight', async () => {
    mockGet.mockResolvedValue({ data: [BOOK_1] });
    mockPatch.mockReturnValue(new Promise(() => {}));
    const { getByLabelText } = await render(<WishlistScreen />);
    await waitFor(() => expect(getByLabelText('Mark Dune as bought')).toBeTruthy());

    fireEvent.press(getByLabelText('Mark Dune as bought'));
    await waitFor(() =>
      expect(getByLabelText('Mark Dune as bought').props.accessibilityState.disabled).toBe(true)
    );
    fireEvent.press(getByLabelText('Mark Dune as bought'));

    expect(mockPatch).toHaveBeenCalledTimes(1);
    expect(mockDelete).not.toHaveBeenCalled();
  });

  it('keeps the book, alerts, and unlocks its buttons when PATCH fails', async () => {
    const alertSpy = jest.spyOn(Alert, 'alert').mockImplementation(() => {});
    mockPatch.mockRejectedValue(new Error('Network error'));
    mockGet.mockResolvedValue({ data: [BOOK_1] });
    const screen = await render(<WishlistScreen />);
    const { getByLabelText } = screen;
    const queryByText = (text: string) => rowTexts(screen)[0]?.queryByText(text, HIDDEN) ?? null;
    await waitFor(() => expect(getByLabelText('Mark Dune as bought')).toBeTruthy());

    await act(async () => {
      await fireEvent.press(getByLabelText('Mark Dune as bought'));
    });

    expect(alertSpy).toHaveBeenCalledTimes(1);
    expect(queryByText('Dune')).toBeTruthy();
    expect(getByLabelText('Mark Dune as bought').props.accessibilityState.disabled).toBe(false);
  });

  it('calls DELETE and removes book when Remove pressed', async () => {
    mockGet.mockResolvedValue({ data: [BOOK_1] });
    const screen = await render(<WishlistScreen />);
    const { getByLabelText } = screen;
    const queryByText = (text: string) => rowTexts(screen)[0]?.queryByText(text, HIDDEN) ?? null;
    await waitFor(() => expect(getByLabelText('Dune — Wishlist, by Frank Herbert')).toBeTruthy());
    await fireEvent.press(getByLabelText('Dune — Wishlist, by Frank Herbert'));

    await act(async () => {
      await fireEvent.press(getByLabelText('Remove Dune'));
    });

    expect(mockDelete).toHaveBeenCalledWith('/user-books/ub-1');
    await waitFor(() => expect(queryByText('Dune')).toBeNull());
  });

  it('keeps the book until DELETE resolves, then removes it', async () => {
    mockGet.mockResolvedValue({ data: [BOOK_1] });
    const screen = await render(<WishlistScreen />);
    const { getByLabelText } = screen;
    const queryByText = (text: string) => rowTexts(screen)[0]?.queryByText(text, HIDDEN) ?? null;

    let resolveDelete!: () => void;
    mockDelete.mockReturnValue(
      new Promise((resolve) => {
        resolveDelete = () => resolve({});
      })
    );

    await waitFor(() => expect(getByLabelText('Dune — Wishlist, by Frank Herbert')).toBeTruthy());
    await fireEvent.press(getByLabelText('Dune — Wishlist, by Frank Herbert'));

    // Deliberately not awaited — see the analogous PATCH test above for why.
    fireEvent.press(getByLabelText('Remove Dune'));

    await waitFor(() =>
      expect(getByLabelText('Remove Dune').props.accessibilityState.disabled).toBe(true)
    );
    expect(queryByText('Dune')).toBeTruthy();

    await act(async () => {
      resolveDelete();
    });
    await waitFor(() => expect(queryByText('Dune')).toBeNull());
  });

  it('keeps the book and alerts when DELETE fails', async () => {
    const alertSpy = jest.spyOn(Alert, 'alert').mockImplementation(() => {});
    mockDelete.mockRejectedValue(new Error('Network error'));
    mockGet.mockResolvedValue({ data: [BOOK_1] });
    const screen = await render(<WishlistScreen />);
    const { getByLabelText } = screen;
    const queryByText = (text: string) => rowTexts(screen)[0]?.queryByText(text, HIDDEN) ?? null;
    await waitFor(() => expect(getByLabelText('Dune — Wishlist, by Frank Herbert')).toBeTruthy());
    await fireEvent.press(getByLabelText('Dune — Wishlist, by Frank Herbert'));

    await act(async () => {
      await fireEvent.press(getByLabelText('Remove Dune'));
    });

    expect(alertSpy).toHaveBeenCalledTimes(1);
    expect(queryByText('Dune')).toBeTruthy();
  });

  it('fetches with status=wishlisted filter', async () => {
    mockGet.mockResolvedValue({ data: [] });
    await render(<WishlistScreen />);
    await waitFor(() =>
      expect(mockGet).toHaveBeenCalledWith('/user-books', { params: { status: 'wishlisted' } })
    );
  });

  it('shows the service-unavailable message on 403 forbidden (#724)', async () => {
    const alertSpy = jest.spyOn(Alert, 'alert').mockImplementation(() => {});
    mockGet.mockRejectedValue({ response: { status: 403, data: { detail: 'forbidden' } } });
    await render(<WishlistScreen />);
    await waitFor(() => expect(alertSpy).toHaveBeenCalledTimes(1));
    expect(alertSpy.mock.calls[0][1]).toBe(
      "Can't reach BookshelfAI right now. Please try again later."
    );
  });

  it('keeps the generic load error for other failures (#724)', async () => {
    const alertSpy = jest.spyOn(Alert, 'alert').mockImplementation(() => {});
    mockGet.mockRejectedValue({ response: { status: 500, data: { detail: 'oops' } } });
    await render(<WishlistScreen />);
    await waitFor(() => expect(alertSpy).toHaveBeenCalledTimes(1));
    expect(alertSpy.mock.calls[0][1]).not.toMatch(/Can't reach BookshelfAI/);
  });

  it('locks Remove in the sheet while a Bought request for that book is in flight', async () => {
    mockGet.mockResolvedValue({ data: [BOOK_1] });
    mockPatch.mockReturnValue(new Promise(() => {}));
    const { getByLabelText } = await render(<WishlistScreen />);
    await waitFor(() => expect(getByLabelText('Mark Dune as bought')).toBeTruthy());

    fireEvent.press(getByLabelText('Mark Dune as bought'));
    await waitFor(() =>
      expect(getByLabelText('Mark Dune as bought').props.accessibilityState.disabled).toBe(true)
    );
    await fireEvent.press(getByLabelText('Dune — Wishlist, by Frank Herbert'));
    await waitFor(() => expect(getByLabelText('Remove Dune')).toBeTruthy());
    expect(getByLabelText('Remove Dune').props.accessibilityState.disabled).toBe(true);
    fireEvent.press(getByLabelText('Remove Dune'));

    expect(mockPatch).toHaveBeenCalledTimes(1);
    expect(mockDelete).not.toHaveBeenCalled();
  });

  it('says "Unverified" in the row label of an unverified entry (#616)', async () => {
    const unverified = {
      ...BOOK_1,
      id: 'ub-u',
      unverified: true,
      book: { ...BOOK_1.book, id: 'b-u', title: 'Pond Notes', author: 'A. Local' },
    };
    mockGet.mockResolvedValue({ data: [unverified, BOOK_1] });
    const { findByLabelText } = await render(<WishlistScreen />);
    const row = await findByLabelText('Pond Notes — Wishlist, by A. Local — Unverified');
    expect(row.props.accessibilityRole).toBe('button');
    // A catalog book's label is unchanged.
    expect(await findByLabelText('Dune — Wishlist, by Frank Herbert')).toBeTruthy();
  });
});

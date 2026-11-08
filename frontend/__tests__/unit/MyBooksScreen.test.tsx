import React from 'react';
import { Alert, Platform } from 'react-native';
import { act, fireEvent, render, waitFor, within } from '@testing-library/react-native';

import { IOS_DISMISS_FALLBACK_MS } from '../../hooks/useModalHandoff';
import MyBooksScreen from '../../app/(tabs)/my-books';
import { userBookRow, routerNavigate } from '../helpers/screenMocks';
import { notifyLibraryChanged } from '../../lib/libraryChanged';

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

jest.mock('expo-router', () => require('../helpers/screenMocks').focusEffectRouterModule());
jest.mock('../../hooks/useTheme', () => require('../helpers/screenMocks').themeModule());
jest.mock('../../components/LoadingSpinner', () =>
  require('../helpers/screenMocks').loadingSpinnerModule()
);

const WISHLISTED_BOOK = userBookRow();

/** Load the list with one wishlisted book and open its detail sheet. */
async function openWishlistedSheet() {
  mockGet.mockResolvedValue({ data: [WISHLISTED_BOOK] });
  const screen = await render(<MyBooksScreen />);
  await waitFor(() => screen.getByLabelText('Dune — Wishlist, by Frank Herbert'));
  await fireEvent.press(screen.getByLabelText('Dune — Wishlist, by Frank Herbert'));
  return screen;
}

beforeEach(() => {
  jest.clearAllMocks();
  mockGet.mockResolvedValue({ data: [] });
  mockPatch.mockResolvedValue({ data: {} });
  mockDelete.mockResolvedValue({});
});

describe('MyBooksScreen', () => {
  it('shows loading spinner on mount', async () => {
    mockGet.mockReturnValue(new Promise(() => {}));
    const { getByTestId } = await render(<MyBooksScreen />);
    expect(getByTestId('loading-spinner')).toBeTruthy();
  });

  it('shows empty state when no books', async () => {
    mockGet.mockResolvedValue({ data: [] });
    const { getByText } = await render(<MyBooksScreen />);
    await waitFor(() => expect(getByText('Your library is empty')).toBeTruthy(), {
      timeout: 10_000,
    });
  }, 15_000);

  it('renders all filter chips with their counts', async () => {
    mockGet.mockResolvedValue({ data: [WISHLISTED_BOOK] });
    const { getByLabelText } = await render(<MyBooksScreen />);
    await waitFor(() => {
      expect(getByLabelText('Filter by All, 1 book')).toBeTruthy();
      expect(getByLabelText('Filter by Wishlist, 1 book')).toBeTruthy();
      expect(getByLabelText('Filter by Purchased, 0 books')).toBeTruthy();
      expect(getByLabelText('Filter by Reading, 0 books')).toBeTruthy();
      expect(getByLabelText('Filter by Read, 0 books')).toBeTruthy();
    });
  });

  it('renders book titles when data loaded', async () => {
    mockGet.mockResolvedValue({ data: [WISHLISTED_BOOK] });
    const { getByText, getByTestId } = await render(<MyBooksScreen />);
    await waitFor(() => {
      // Scoped to the row's text block: the generated cover also typesets the title.
      const text = within(getByTestId('book-row-text', { hidden: true }));
      expect(text.getByText('Dune', { hidden: true })).toBeTruthy();
      expect(getByText('Frank Herbert', { hidden: true })).toBeTruthy();
    });
  });

  it('shows the status (the same string as the tab chip) and year on each row', async () => {
    mockGet.mockResolvedValue({ data: [WISHLISTED_BOOK] });
    const { getByText } = await render(<MyBooksScreen />);
    await waitFor(() => expect(getByText('Wishlist · 1965', { hidden: true })).toBeTruthy());
  });

  it('opens detail sheet when card is tapped', async () => {
    mockGet.mockResolvedValue({ data: [WISHLISTED_BOOK] });
    const { getByLabelText, getByText } = await render(<MyBooksScreen />);
    await waitFor(() => getByLabelText('Dune — Wishlist, by Frank Herbert'));

    await fireEvent.press(getByLabelText('Dune — Wishlist, by Frank Herbert'));

    await waitFor(() => expect(getByText('A desert planet epic.')).toBeTruthy());
  });

  it('shows page count in detail sheet', async () => {
    const { getByText } = await openWishlistedSheet();
    await waitFor(() => expect(getByText('604 pages')).toBeTruthy());
  });

  it('closes detail sheet when Close pressed', async () => {
    const { getByLabelText, queryByText } = await openWishlistedSheet();
    await waitFor(() => getByLabelText('Close detail'));
    await fireEvent.press(getByLabelText('Close detail'));
    await waitFor(() => expect(queryByText('A desert planet epic.')).toBeNull());
  });

  it('shows advance status button for wishlisted books', async () => {
    const { getByLabelText } = await openWishlistedSheet();
    await waitFor(() => expect(getByLabelText('Mark as Purchased')).toBeTruthy());
  });

  it('calls PATCH when advance status button pressed', async () => {
    const { getByLabelText } = await openWishlistedSheet();
    await waitFor(() => getByLabelText('Mark as Purchased'));

    await act(async () => {
      await fireEvent.press(getByLabelText('Mark as Purchased'));
    });

    expect(mockPatch).toHaveBeenCalledWith('/user-books/ub-1', { status: 'purchased' });
  });

  it('does not change status until PATCH resolves, then applies the server row', async () => {
    let resolvePatch!: (row: unknown) => void;
    mockPatch.mockReturnValue(
      new Promise((resolve) => {
        resolvePatch = (row) => resolve({ data: row });
      })
    );
    const { getByLabelText, queryByLabelText } = await openWishlistedSheet();
    await waitFor(() => getByLabelText('Mark as Purchased'));

    // Deliberately not awaited: under v14, awaiting fireEvent.press blocks
    // until all work the event triggers has settled, which would hang here
    // on the still-pending PATCH call.
    fireEvent.press(getByLabelText('Mark as Purchased'));

    // Server-authoritative: still wishlisted while the request is in flight,
    // and the sheet's buttons are locked. The sheet stays open, which hides the
    // cards behind it from default queries.
    await waitFor(() =>
      expect(getByLabelText('Mark as Purchased').props.accessibilityState.disabled).toBe(true)
    );
    const hidden = { includeHiddenElements: true };
    expect(queryByLabelText('Dune — Wishlist, by Frank Herbert', hidden)).toBeTruthy();
    expect(queryByLabelText('Dune — Purchased, by Frank Herbert', hidden)).toBeNull();

    await act(async () => {
      resolvePatch({
        ...WISHLISTED_BOOK,
        status: 'purchased',
        purchased_at: '2026-09-21T00:00:00Z',
      });
    });
    // On success the sheet closes and the card shows the server's status.
    await waitFor(() =>
      expect(queryByLabelText('Dune — Purchased, by Frank Herbert')).toBeTruthy()
    );
    expect(queryByLabelText('Dune — Wishlist, by Frank Herbert', { hidden: true })).toBeNull();
  });

  it('keeps the advanced book when the user has moved to its destination chip (#641)', async () => {
    let resolvePatch!: (row: unknown) => void;
    mockPatch.mockReturnValue(
      new Promise((resolve) => {
        resolvePatch = (row) => resolve({ data: row });
      })
    );
    const purchased = { ...WISHLISTED_BOOK, status: 'purchased' };
    mockGet.mockResolvedValueOnce({ data: [WISHLISTED_BOOK] });
    const { getByLabelText, queryByLabelText } = await render(<MyBooksScreen />);
    await waitFor(() => getByLabelText('Dune — Wishlist, by Frank Herbert'));
    await fireEvent.press(getByLabelText('Dune — Wishlist, by Frank Herbert'));
    await waitFor(() => getByLabelText('Mark as Purchased'));

    // Deliberately not awaited: the PATCH stays pending (see the test above).
    fireEvent.press(getByLabelText('Mark as Purchased'));
    await waitFor(() =>
      expect(getByLabelText('Mark as Purchased').props.accessibilityState.disabled).toBe(true)
    );

    // While it is in flight the user switches to the Purchased chip. That is a
    // client-side filter now: no request, and the still-wishlisted row leaves the view.
    const hidden = { includeHiddenElements: true };
    await act(async () => {
      await fireEvent.press(getByLabelText(/^Filter by Purchased/, hidden));
    });
    expect(mockGet).toHaveBeenCalledTimes(1);
    // The row is still wishlisted, so it is not in the Purchased view.
    expect(queryByLabelText('Dune — Wishlist, by Frank Herbert', hidden)).toBeNull();

    await act(async () => {
      resolvePatch({ ...purchased, purchased_at: '2026-09-21T00:00:00Z' });
    });
    // The book now belongs to this chip, so it appears (updated), not vanishes.
    await waitFor(() =>
      expect(queryByLabelText('Dune — Purchased, by Frank Herbert')).toBeTruthy()
    );
  });

  it('ignores a second press while a request for the same book is in flight', async () => {
    mockPatch.mockReturnValue(new Promise(() => {}));
    const { getByLabelText } = await openWishlistedSheet();
    await waitFor(() => getByLabelText('Mark as Purchased'));

    fireEvent.press(getByLabelText('Mark as Purchased'));
    await waitFor(() =>
      expect(getByLabelText('Mark as Purchased').props.accessibilityState.disabled).toBe(true)
    );
    fireEvent.press(getByLabelText('Mark as Purchased'));
    expect(getByLabelText('Remove Dune').props.accessibilityState.disabled).toBe(true);
    fireEvent.press(getByLabelText('Remove Dune'));

    expect(mockPatch).toHaveBeenCalledTimes(1);
    expect(mockDelete).not.toHaveBeenCalled();
  });

  it('keeps the status and alerts when PATCH fails', async () => {
    const alertSpy = jest.spyOn(Alert, 'alert').mockImplementation(() => {});
    mockPatch.mockRejectedValue(new Error('Network error'));
    const { getByLabelText, queryByLabelText } = await openWishlistedSheet();
    await waitFor(() => getByLabelText('Mark as Purchased'));

    await act(async () => {
      await fireEvent.press(getByLabelText('Mark as Purchased'));
    });

    expect(alertSpy).toHaveBeenCalledTimes(1);
    // The sheet stays open so the user can retry; its buttons are unlocked again.
    const hidden = { includeHiddenElements: true };
    expect(queryByLabelText('Dune — Purchased, by Frank Herbert', hidden)).toBeNull();
    expect(queryByLabelText('Dune — Wishlist, by Frank Herbert', hidden)).toBeTruthy();
    expect(getByLabelText('Mark as Purchased').props.accessibilityState.disabled).toBe(false);
  });

  it('does not show advance button for read books', async () => {
    const readBook = { ...WISHLISTED_BOOK, status: 'read' };
    mockGet.mockResolvedValue({ data: [readBook] });
    const { getByLabelText, queryByLabelText } = await render(<MyBooksScreen />);
    await waitFor(() => getByLabelText('Dune — Read, by Frank Herbert'));
    await fireEvent.press(getByLabelText('Dune — Read, by Frank Herbert'));
    await waitFor(() => expect(queryByLabelText(/Mark as/)).toBeNull());
  });

  it('calls DELETE when Remove pressed in detail sheet', async () => {
    const { getByLabelText } = await openWishlistedSheet();
    await waitFor(() => getByLabelText('Remove Dune'));

    await act(async () => {
      await fireEvent.press(getByLabelText('Remove Dune'));
    });

    expect(mockDelete).toHaveBeenCalledWith('/user-books/ub-1');
  });

  it('keeps the book until DELETE resolves, then removes it', async () => {
    let resolveDelete!: () => void;
    mockDelete.mockReturnValue(
      new Promise((resolve) => {
        resolveDelete = () => resolve({});
      })
    );
    const { getByLabelText, queryByLabelText } = await openWishlistedSheet();
    await waitFor(() => getByLabelText('Remove Dune'));

    // Deliberately not awaited — see the analogous PATCH test above for why.
    fireEvent.press(getByLabelText('Remove Dune'));

    await waitFor(() =>
      expect(getByLabelText('Remove Dune').props.accessibilityState.disabled).toBe(true)
    );
    expect(queryByLabelText('Dune — Wishlist, by Frank Herbert', { hidden: true })).toBeTruthy();

    await act(async () => {
      resolveDelete();
    });
    await waitFor(() =>
      expect(queryByLabelText('Dune — Wishlist, by Frank Herbert', { hidden: true })).toBeNull()
    );
  });

  it('keeps the book and alerts when DELETE fails', async () => {
    const alertSpy = jest.spyOn(Alert, 'alert').mockImplementation(() => {});
    mockDelete.mockRejectedValue(new Error('Network error'));
    const { getByLabelText, queryByLabelText } = await openWishlistedSheet();
    await waitFor(() => getByLabelText('Remove Dune'));

    await act(async () => {
      await fireEvent.press(getByLabelText('Remove Dune'));
    });

    expect(alertSpy).toHaveBeenCalledTimes(1);
    expect(queryByLabelText('Dune — Wishlist, by Frank Herbert', { hidden: true })).toBeTruthy();
  });

  it('filters on the client when a chip is tapped, without a new request', async () => {
    mockGet.mockResolvedValue({ data: [WISHLISTED_BOOK] });
    const { getByLabelText, queryByLabelText } = await render(<MyBooksScreen />);
    await waitFor(() => getByLabelText('Dune — Wishlist, by Frank Herbert'));
    await act(async () => {
      await fireEvent.press(getByLabelText('Filter by Reading, 0 books'));
    });
    expect(queryByLabelText('Dune — Wishlist, by Frank Herbert')).toBeNull();
    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(mockGet).toHaveBeenCalledWith('/user-books');
  });

  it('shows the service-unavailable message on 403 forbidden (#724)', async () => {
    const alertSpy = jest.spyOn(Alert, 'alert').mockImplementation(() => {});
    mockGet.mockRejectedValue({ response: { status: 403, data: { detail: 'forbidden' } } });
    await render(<MyBooksScreen />);
    await waitFor(() => expect(alertSpy).toHaveBeenCalledTimes(1));
    expect(alertSpy.mock.calls[0][1]).toBe(
      "Can't reach BookshelfAI right now. Please try again later."
    );
  });

  it('keeps the generic load error for other failures (#724)', async () => {
    const alertSpy = jest.spyOn(Alert, 'alert').mockImplementation(() => {});
    mockGet.mockRejectedValue({ response: { status: 500, data: { detail: 'oops' } } });
    await render(<MyBooksScreen />);
    await waitFor(() => expect(alertSpy).toHaveBeenCalledTimes(1));
    expect(alertSpy.mock.calls[0][1]).not.toMatch(/Can't reach BookshelfAI/);
  });
});

describe('MyBooksScreen row menu (#929)', () => {
  async function openMenu() {
    mockGet.mockResolvedValue({ data: [WISHLISTED_BOOK] });
    const screen = await render(<MyBooksScreen />);
    await waitFor(() => screen.getByLabelText('More options for Dune'));
    await fireEvent.press(screen.getByLabelText('More options for Dune'));
    return screen;
  }

  it('offers the next status, View details and Remove from library', async () => {
    const { findByLabelText } = await openMenu();
    expect(await findByLabelText('Mark as Purchased')).toBeTruthy();
    expect(await findByLabelText('View details')).toBeTruthy();
    expect(await findByLabelText('Remove from library')).toBeTruthy();
  });

  it('advances the status through PATCH', async () => {
    const { findByLabelText } = await openMenu();
    await act(async () => {
      await fireEvent.press(await findByLabelText('Mark as Purchased'));
    });
    expect(mockPatch).toHaveBeenCalledWith('/user-books/ub-1', { status: 'purchased' });
  });

  it('removes through DELETE', async () => {
    const { findByLabelText } = await openMenu();
    await act(async () => {
      await fireEvent.press(await findByLabelText('Remove from library'));
    });
    expect(mockDelete).toHaveBeenCalledWith('/user-books/ub-1');
  });

  it('opens the detail sheet from View details (Android/web: at once)', async () => {
    const original = Platform.OS;
    Platform.OS = 'android';
    try {
      const { findByLabelText, findByText } = await openMenu();
      await fireEvent.press(await findByLabelText('View details'));
      expect(await findByText('A desert planet epic.')).toBeTruthy();
    } finally {
      Platform.OS = original;
    }
  });

  describe('iOS: View details waits for the menu to dismiss', () => {
    const DETAIL = 'A desert planet epic.';
    const EMMA = userBookRow({
      id: 'ub-2',
      book: { id: 'b-2', title: 'Emma', author: 'Jane Austen', cover_url: null, description: 'x' },
    });

    afterEach(() => jest.useRealTimers());

    /**
     * The menu Modal's onDismiss (SheetModal passes its onHidden). Read it while the
     * menu is open: a closed Modal renders nothing.
     */
    const menuOnDismiss = (screen: Awaited<ReturnType<typeof openMenu>>) => {
      let node = screen.getByTestId('sheet-scrim').parent;
      while (node && typeof node.props.onDismiss !== 'function') node = node.parent;
      return node!.props.onDismiss as () => void;
    };

    it('opens the detail sheet when the menu Modal reports onDismiss', async () => {
      const screen = await openMenu();
      const dismiss = menuOnDismiss(screen);
      const details = await screen.findByLabelText('View details');
      // Fake timers, never advanced: only onDismiss can open the sheet, not the fallback.
      jest.useFakeTimers();
      await fireEvent.press(details);
      // The menu is gone but its dismissal has not been reported: nothing opens yet.
      expect(screen.queryByText(DETAIL)).toBeNull();
      await act(async () => dismiss());
      expect(screen.getByText(DETAIL)).toBeTruthy();
    });

    it('opens the detail sheet after the fallback when onDismiss never fires', async () => {
      const screen = await openMenu();
      const details = await screen.findByLabelText('View details');
      // Fake timers first, so the fallback timer armed by the press is a fake one.
      jest.useFakeTimers();
      await fireEvent.press(details);
      await act(async () => {
        jest.advanceTimersByTime(IOS_DISMISS_FALLBACK_MS - 1);
      });
      expect(screen.queryByText(DETAIL)).toBeNull();
      await act(async () => {
        jest.advanceTimersByTime(IOS_DISMISS_FALLBACK_MS);
      });
      expect(await screen.findByText(DETAIL)).toBeTruthy();
    });

    it('never pops a stale detail sheet up later', async () => {
      mockGet.mockResolvedValue({ data: [WISHLISTED_BOOK, EMMA] });
      const screen = await render(<MyBooksScreen />);
      await waitFor(() => screen.getByLabelText('More options for Dune'));
      await fireEvent.press(screen.getByLabelText('More options for Dune'));
      const dismissDune = menuOnDismiss(screen);
      await fireEvent.press(await screen.findByLabelText('View details'));
      // Before the handoff completes the user opens another book's menu.
      await fireEvent.press(await screen.findByLabelText('More options for Emma'));
      await act(async () => dismissDune());
      // Close the Emma menu: neither Dune's nor anything else's detail appears.
      const dismissEmma = menuOnDismiss(screen);
      await fireEvent.press(await screen.findByLabelText('Close'));
      await act(async () => dismissEmma());
      expect(screen.queryByText(DETAIL)).toBeNull();
      expect(screen.queryByLabelText('Close detail')).toBeNull();
    });
  });
});

describe('MyBooksScreen header, search and empty states (#930)', () => {
  const EMMA = userBookRow({
    id: 'ub-2',
    status: 'reading',
    book: { id: 'b-2', title: 'Emma', author: 'Jane Austen', cover_url: null, description: 'x' },
  });
  const GATSBY = userBookRow({
    id: 'ub-3',
    status: 'reading',
    book: {
      id: 'b-3',
      title: 'The Great Gatsby',
      author: 'F. Scott',
      cover_url: null,
      description: 'x',
    },
  });

  it('titles the screen and counts the books and the ones being read', async () => {
    mockGet.mockResolvedValue({ data: [WISHLISTED_BOOK, EMMA, GATSBY] });
    const { getByRole, getByTestId } = await render(<MyBooksScreen />);
    await waitFor(() => expect(getByRole('header', { name: 'My Books' })).toBeTruthy());
    expect(getByTestId('my-books-subtitle').props.children).toBe('3 books · 2 reading now');
  });

  it('leaves the reading part out when nothing is being read, and pluralises', async () => {
    mockGet.mockResolvedValue({ data: [WISHLISTED_BOOK] });
    const { getByTestId } = await render(<MyBooksScreen />);
    await waitFor(() => expect(getByTestId('my-books-subtitle').props.children).toBe('1 book'));
  });

  it('makes exactly one request per focus, however often it re-renders', async () => {
    mockGet.mockResolvedValue({ data: [WISHLISTED_BOOK] });
    const { getByLabelText } = await render(<MyBooksScreen />);
    await waitFor(() => getByLabelText('Dune — Wishlist, by Frank Herbert'));
    await fireEvent.changeText(getByLabelText('Search your books'), 'dun');
    await fireEvent.press(getByLabelText('Filter by Wishlist, 1 book'));
    await fireEvent.changeText(getByLabelText('Search your books'), '');
    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(mockGet).toHaveBeenCalledWith('/user-books');
  });

  it('reloads when the library changes elsewhere', async () => {
    mockGet.mockResolvedValue({ data: [WISHLISTED_BOOK] });
    const { getByLabelText } = await render(<MyBooksScreen />);
    await waitFor(() => getByLabelText('Dune — Wishlist, by Frank Herbert'));
    await act(async () => notifyLibraryChanged());
    expect(mockGet).toHaveBeenCalledTimes(2);
  });

  it('keeps the newest response when two requests overlap', async () => {
    let resolveFirst!: (v: unknown) => void;
    mockGet.mockReturnValueOnce(new Promise((r) => (resolveFirst = r)));
    mockGet.mockResolvedValueOnce({ data: [EMMA] });
    const { getByLabelText, queryByLabelText } = await render(<MyBooksScreen />);
    await act(async () => notifyLibraryChanged());
    await waitFor(() => getByLabelText('Emma — Reading, by Jane Austen'));
    await act(async () => resolveFirst({ data: [WISHLISTED_BOOK] }));
    expect(queryByLabelText('Dune — Wishlist, by Frank Herbert')).toBeNull();
    expect(getByLabelText('Emma — Reading, by Jane Austen')).toBeTruthy();
  });

  it('matches title and author case-insensitively, together with the chip', async () => {
    mockGet.mockResolvedValue({ data: [WISHLISTED_BOOK, EMMA, GATSBY] });
    const { getByLabelText, queryByLabelText } = await render(<MyBooksScreen />);
    await waitFor(() => getByLabelText('Emma — Reading, by Jane Austen'));
    await fireEvent.changeText(getByLabelText('Search your books'), 'AUSTEN');
    expect(queryByLabelText('Dune — Wishlist, by Frank Herbert')).toBeNull();
    expect(getByLabelText('Emma — Reading, by Jane Austen')).toBeTruthy();
    // Chip and search narrow together; counts keep counting every book.
    await fireEvent.changeText(getByLabelText('Search your books'), 'the');
    await fireEvent.press(getByLabelText('Filter by Wishlist, 1 book'));
    expect(queryByLabelText('The Great Gatsby — Reading, by F. Scott')).toBeNull();
    expect(getByLabelText('Filter by Reading, 2 books')).toBeTruthy();
  });

  it('moving the last book out of a chip shows its empty state; the book is under its new chip (#641)', async () => {
    mockPatch.mockResolvedValue({ data: { ...WISHLISTED_BOOK, status: 'purchased' } });
    mockGet.mockResolvedValue({ data: [WISHLISTED_BOOK] });
    const { getByLabelText, findByText, findByLabelText } = await render(<MyBooksScreen />);
    await waitFor(() => getByLabelText('Dune — Wishlist, by Frank Herbert'));
    await fireEvent.press(getByLabelText('Filter by Wishlist, 1 book'));
    await fireEvent.press(getByLabelText('Dune — Wishlist, by Frank Herbert'));
    await act(async () => {
      await fireEvent.press(await findByLabelText('Mark as Purchased'));
    });
    expect(await findByText('Nothing marked wishlisted yet')).toBeTruthy();
    await fireEvent.press(await findByLabelText('Filter by Purchased, 1 book'));
    expect(await findByLabelText('Dune — Purchased, by Frank Herbert')).toBeTruthy();
  });

  describe('a GET that was in flight when a mutation finished is dropped', () => {
    async function withSlowReload() {
      mockGet.mockResolvedValueOnce({ data: [WISHLISTED_BOOK] });
      const screen = await render(<MyBooksScreen />);
      await waitFor(() => screen.getByLabelText('Dune — Wishlist, by Frank Herbert'));
      let resolveGet!: (v: unknown) => void;
      mockGet.mockReturnValueOnce(new Promise((r) => (resolveGet = r)));
      await act(async () => notifyLibraryChanged());
      await fireEvent.press(screen.getByLabelText('Dune — Wishlist, by Frank Herbert'));
      return { screen, resolveStale: () => resolveGet({ data: [WISHLISTED_BOOK] }) };
    }

    it('does not undo an advance', async () => {
      mockPatch.mockResolvedValue({ data: { ...WISHLISTED_BOOK, status: 'purchased' } });
      const { screen, resolveStale } = await withSlowReload();
      // The reload that replaces the dropped GET sees the server after the advance.
      mockGet.mockResolvedValueOnce({ data: [{ ...WISHLISTED_BOOK, status: 'purchased' }] });
      await act(async () => {
        await fireEvent.press(await screen.findByLabelText('Mark as Purchased'));
      });
      await screen.findByLabelText('Dune — Purchased, by Frank Herbert');
      await act(async () => resolveStale());
      expect(screen.getByLabelText('Dune — Purchased, by Frank Herbert')).toBeTruthy();
      expect(screen.queryByLabelText('Dune — Wishlist, by Frank Herbert')).toBeNull();
    });

    it('still lands a refresh that was in flight: the newest data wins, the advance stays', async () => {
      mockPatch.mockResolvedValue({ data: { ...WISHLISTED_BOOK, status: 'purchased' } });
      const { screen, resolveStale } = await withSlowReload();
      // The reload that replaces the dropped one sees the server after the advance.
      mockGet.mockResolvedValueOnce({
        data: [{ ...WISHLISTED_BOOK, status: 'purchased' }, EMMA],
      });
      await act(async () => {
        await fireEvent.press(await screen.findByLabelText('Mark as Purchased'));
      });
      expect(await screen.findByLabelText('Emma — Reading, by Jane Austen')).toBeTruthy();
      await act(async () => resolveStale());
      expect(screen.getByLabelText('Dune — Purchased, by Frank Herbert')).toBeTruthy();
      expect(screen.queryByLabelText('Dune — Wishlist, by Frank Herbert')).toBeNull();
      expect(screen.getByLabelText('Emma — Reading, by Jane Austen')).toBeTruthy();
    });

    it('does not bring a removed book back', async () => {
      const { screen, resolveStale } = await withSlowReload();
      await act(async () => {
        await fireEvent.press(await screen.findByLabelText('Remove Dune'));
      });
      await screen.findByText('Your library is empty');
      await act(async () => resolveStale());
      expect(screen.queryByLabelText('Dune — Wishlist, by Frank Herbert')).toBeNull();
      expect(screen.getByText('Your library is empty')).toBeTruthy();
    });
  });

  it('a failed first load is not an empty library: it offers Try again', async () => {
    jest.spyOn(Alert, 'alert').mockImplementation(() => {});
    mockGet.mockRejectedValueOnce({ response: { status: 500, data: {} } });
    mockGet.mockResolvedValue({ data: [WISHLISTED_BOOK] });
    const { findByText, queryByText, getByText, findByLabelText } = await render(<MyBooksScreen />);
    expect(await findByText('Could not load books.')).toBeTruthy();
    expect(queryByText('Your library is empty')).toBeNull();
    await fireEvent.press(getByText('Try again'));
    expect(await findByLabelText('Dune — Wishlist, by Frank Herbert')).toBeTruthy();
  });

  it('a failed background reload does not hide the empty library after the last book is removed', async () => {
    const alertSpy = jest.spyOn(Alert, 'alert').mockImplementation(() => {});
    mockGet.mockResolvedValueOnce({ data: [WISHLISTED_BOOK] });
    const { getByLabelText, findByLabelText, findByText, queryByText } = await render(
      <MyBooksScreen />
    );
    await waitFor(() => getByLabelText('Dune — Wishlist, by Frank Herbert'));
    mockGet.mockRejectedValueOnce({ response: { status: 500, data: {} } });
    await act(async () => notifyLibraryChanged());
    await waitFor(() => expect(alertSpy).toHaveBeenCalledTimes(1));
    await fireEvent.press(getByLabelText('Dune — Wishlist, by Frank Herbert'));
    await act(async () => {
      await fireEvent.press(await findByLabelText('Remove Dune'));
    });
    expect(await findByText('Your library is empty')).toBeTruthy();
    expect(queryByText('Could not load books.')).toBeNull();
  });

  it('empty library: offers Scan books and hides the search and chips', async () => {
    mockGet.mockResolvedValue({ data: [] });
    const { findByText, getByText, queryByLabelText } = await render(<MyBooksScreen />);
    expect(await findByText('Your library is empty')).toBeTruthy();
    expect(queryByLabelText('Search your books')).toBeNull();
    await fireEvent.press(getByText('Scan books'));
    expect(routerNavigate).toHaveBeenCalledWith('/(tabs)/scan');
  });

  it('empty filter: names the status and Show all books returns to All', async () => {
    mockGet.mockResolvedValue({ data: [WISHLISTED_BOOK] });
    const { getByLabelText, findByText, getByText } = await render(<MyBooksScreen />);
    await waitFor(() => getByLabelText('Dune — Wishlist, by Frank Herbert'));
    await fireEvent.press(getByLabelText('Filter by Reading, 0 books'));
    expect(await findByText('Nothing marked reading yet')).toBeTruthy();
    // The search field and chips stay.
    expect(getByLabelText('Search your books')).toBeTruthy();
    await fireEvent.press(getByText('Show all books'));
    expect(getByLabelText('Dune — Wishlist, by Frank Herbert')).toBeTruthy();
  });

  it('empty search: quotes the query and Clear search brings the list back', async () => {
    mockGet.mockResolvedValue({ data: [WISHLISTED_BOOK] });
    const { getByLabelText, findByText, getByText } = await render(<MyBooksScreen />);
    await waitFor(() => getByLabelText('Dune — Wishlist, by Frank Herbert'));
    await fireEvent.changeText(getByLabelText('Search your books'), 'zzz');
    expect(await findByText('No books match “zzz”')).toBeTruthy();
    expect(getByLabelText('Filter by All, 1 book')).toBeTruthy();
    await fireEvent.press(getByText('Clear search'));
    expect(getByLabelText('Dune — Wishlist, by Frank Herbert')).toBeTruthy();
  });
});

import React from 'react';
import { Alert, Platform } from 'react-native';
import { act, fireEvent, render, waitFor, within } from '@testing-library/react-native';

import { IOS_DISMISS_FALLBACK_MS } from '../../hooks/useModalHandoff';
import MyBooksScreen from '../../app/(tabs)/my-books';
import { userBookRow } from '../helpers/screenMocks';

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
    await waitFor(() => expect(getByText('No books here yet.')).toBeTruthy(), {
      timeout: 10_000,
    });
  }, 15_000);

  it('renders all filter tabs', async () => {
    mockGet.mockResolvedValue({ data: [] });
    const { getByText } = await render(<MyBooksScreen />);
    await waitFor(() => {
      expect(getByText('All')).toBeTruthy();
      expect(getByText('Wishlist')).toBeTruthy();
      expect(getByText('Purchased')).toBeTruthy();
      expect(getByText('Reading')).toBeTruthy();
      expect(getByText('Read')).toBeTruthy();
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

  it('keeps the advanced book when the user has moved to its destination tab (#641)', async () => {
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

    // While it is in flight the user switches to the Purchased tab, whose GET
    // already returns the committed, updated row.
    mockGet.mockResolvedValueOnce({ data: [purchased] });
    const hidden = { includeHiddenElements: true };
    await act(async () => {
      await fireEvent.press(getByLabelText('Filter by Purchased', hidden));
    });
    await waitFor(() =>
      expect(queryByLabelText('Dune — Purchased, by Frank Herbert', hidden)).toBeTruthy()
    );

    await act(async () => {
      resolvePatch({ ...purchased, purchased_at: '2026-09-21T00:00:00Z' });
    });
    // The book belongs on this tab, so it must stay (updated), not vanish.
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

  it('refetches when a filter tab is tapped', async () => {
    mockGet.mockResolvedValue({ data: [] });
    const { getByText } = await render(<MyBooksScreen />);
    await waitFor(() => getByText('Reading'));
    await act(async () => {
      await fireEvent.press(getByText('Reading'));
    });
    await waitFor(() =>
      expect(mockGet).toHaveBeenCalledWith('/user-books', { params: { status: 'reading' } })
    );
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
      await fireEvent.press(await screen.findByLabelText('View details'));
      // The menu is gone but its dismissal has not been reported: nothing opens yet.
      expect(screen.queryByText(DETAIL)).toBeNull();
      await act(async () => dismiss());
      expect(await screen.findByText(DETAIL)).toBeTruthy();
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

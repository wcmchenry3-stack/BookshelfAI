import React from 'react';
import { act, fireEvent, render, waitFor } from '@testing-library/react-native';

import MyBooksScreen from '../../app/(tabs)/my-books';

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

jest.mock('../../hooks/useTheme', () => ({
  useTheme: () => ({
    theme: {
      colors: {
        background: '#fff',
        surface: '#f5f5f5',
        border: '#ccc',
        text: '#000',
        textSecondary: '#888',
        primary: '#0f426f',
        onPrimary: '#ffffff',
        secondary: '#47645d',
        onSurface: '#1b1c1a',
        onSurfaceVariant: '#42474f',
        outline: '#737780',
        secondaryContainer: '#c6e7dd',
        onSecondaryContainer: '#4b6861',
        surfaceContainerLow: '#f5f3ef',
        surfaceContainerHigh: '#eae8e4',
        surfaceContainerHighest: '#e4e2de',
      },
      typography: {
        fontSizeBase: 16,
        fontSizeSM: 14,
        fontSizeLG: 20,
        fontSizeXS: 12,
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

const WISHLISTED_BOOK = {
  id: 'ub-1',
  status: 'wishlisted',
  rating: null,
  notes: null,
  wishlisted_at: null,
  purchased_at: null,
  started_at: null,
  finished_at: null,
  book: {
    id: 'b-1',
    title: 'Dune',
    author: 'Frank Herbert',
    cover_url: null,
    description: 'A desert planet epic.',
  },
  edition: { publish_year: 1965, publisher: 'Ace Books', page_count: 604 },
};

const READING_BOOK = {
  ...WISHLISTED_BOOK,
  id: 'ub-2',
  status: 'reading',
  book: { ...WISHLISTED_BOOK.book, id: 'b-2', title: 'Foundation' },
};

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
    const { getByText } = await render(<MyBooksScreen />);
    await waitFor(() => {
      expect(getByText('Dune')).toBeTruthy();
      expect(getByText('Frank Herbert')).toBeTruthy();
    });
  });

  it('renders status badge on each card', async () => {
    mockGet.mockResolvedValue({ data: [WISHLISTED_BOOK] });
    const { getByText } = await render(<MyBooksScreen />);
    await waitFor(() => expect(getByText('Wishlisted')).toBeTruthy());
  });

  it('opens detail sheet when card is tapped', async () => {
    mockGet.mockResolvedValue({ data: [WISHLISTED_BOOK] });
    const { getByLabelText, getByText } = await render(<MyBooksScreen />);
    await waitFor(() => getByLabelText('Dune — wishlisted'));

    await fireEvent.press(getByLabelText('Dune — wishlisted'));

    await waitFor(() => expect(getByText('A desert planet epic.')).toBeTruthy());
  });

  it('shows page count in detail sheet', async () => {
    mockGet.mockResolvedValue({ data: [WISHLISTED_BOOK] });
    const { getByLabelText, getByText } = await render(<MyBooksScreen />);
    await waitFor(() => getByLabelText('Dune — wishlisted'));
    await fireEvent.press(getByLabelText('Dune — wishlisted'));
    await waitFor(() => expect(getByText('604 pages')).toBeTruthy());
  });

  it('closes detail sheet when Close pressed', async () => {
    mockGet.mockResolvedValue({ data: [WISHLISTED_BOOK] });
    const { getByLabelText, queryByText } = await render(<MyBooksScreen />);
    await waitFor(() => getByLabelText('Dune — wishlisted'));
    await fireEvent.press(getByLabelText('Dune — wishlisted'));
    await waitFor(() => getByLabelText('Close detail'));
    await fireEvent.press(getByLabelText('Close detail'));
    await waitFor(() => expect(queryByText('A desert planet epic.')).toBeNull());
  });

  it('shows advance status button for wishlisted books', async () => {
    mockGet.mockResolvedValue({ data: [WISHLISTED_BOOK] });
    const { getByLabelText } = await render(<MyBooksScreen />);
    await waitFor(() => getByLabelText('Dune — wishlisted'));
    await fireEvent.press(getByLabelText('Dune — wishlisted'));
    await waitFor(() => expect(getByLabelText('Mark as Purchased')).toBeTruthy());
  });

  it('calls PATCH when advance status button pressed', async () => {
    mockGet.mockResolvedValue({ data: [WISHLISTED_BOOK] });
    const { getByLabelText } = await render(<MyBooksScreen />);
    await waitFor(() => getByLabelText('Dune — wishlisted'));
    await fireEvent.press(getByLabelText('Dune — wishlisted'));
    await waitFor(() => getByLabelText('Mark as Purchased'));

    await act(async () => {
      await fireEvent.press(getByLabelText('Mark as Purchased'));
    });

    expect(mockPatch).toHaveBeenCalledWith('/user-books/ub-1', { status: 'purchased' });
  });

  it('updates status badge optimistically on "all" tab before PATCH resolves', async () => {
    let resolvePatch!: () => void;
    mockPatch.mockReturnValue(
      new Promise((resolve) => {
        resolvePatch = () => resolve({ data: {} });
      })
    );
    mockGet.mockResolvedValue({ data: [WISHLISTED_BOOK] });
    const { getByLabelText, queryByLabelText } = await render(<MyBooksScreen />);
    await waitFor(() => getByLabelText('Dune — wishlisted'));
    await fireEvent.press(getByLabelText('Dune — wishlisted'));
    await waitFor(() => getByLabelText('Mark as Purchased'));

    // Deliberately not awaited: under v14, awaiting fireEvent.press blocks
    // until all work the event triggers has settled, which would hang here
    // on the still-pending PATCH call — defeating the point of asserting
    // optimistic (pre-resolution) UI state. waitFor below polls independently
    // for the synchronous state update to land.
    fireEvent.press(getByLabelText('Mark as Purchased'));

    // Card a11y label reflects the new status before API resolves
    await waitFor(() => expect(queryByLabelText('Dune — purchased')).toBeTruthy());
    await act(async () => {
      resolvePatch();
    });
  });

  it('reverts status optimistic update when PATCH fails', async () => {
    mockPatch.mockRejectedValue(new Error('Network error'));
    mockGet.mockResolvedValue({ data: [WISHLISTED_BOOK] });
    const { getByLabelText, queryByText } = await render(<MyBooksScreen />);
    await waitFor(() => getByLabelText('Dune — wishlisted'));
    await fireEvent.press(getByLabelText('Dune — wishlisted'));
    await waitFor(() => getByLabelText('Mark as Purchased'));

    await act(async () => {
      await fireEvent.press(getByLabelText('Mark as Purchased'));
    });

    await waitFor(() => expect(queryByText('Wishlisted')).toBeTruthy());
  });

  it('does not show advance button for read books', async () => {
    const readBook = { ...WISHLISTED_BOOK, status: 'read' };
    mockGet.mockResolvedValue({ data: [readBook] });
    const { getByLabelText, queryByLabelText } = await render(<MyBooksScreen />);
    await waitFor(() => getByLabelText('Dune — read'));
    await fireEvent.press(getByLabelText('Dune — read'));
    await waitFor(() => expect(queryByLabelText(/Mark as/)).toBeNull());
  });

  it('calls DELETE when Remove pressed in detail sheet', async () => {
    mockGet.mockResolvedValue({ data: [WISHLISTED_BOOK] });
    const { getByLabelText } = await render(<MyBooksScreen />);
    await waitFor(() => getByLabelText('Dune — wishlisted'));
    await fireEvent.press(getByLabelText('Dune — wishlisted'));
    await waitFor(() => getByLabelText('Remove Dune'));

    await act(async () => {
      await fireEvent.press(getByLabelText('Remove Dune'));
    });

    expect(mockDelete).toHaveBeenCalledWith('/user-books/ub-1');
  });

  it('removes book optimistically before DELETE resolves', async () => {
    let resolveDelete!: () => void;
    mockDelete.mockReturnValue(
      new Promise((resolve) => {
        resolveDelete = () => resolve({});
      })
    );
    mockGet.mockResolvedValue({ data: [WISHLISTED_BOOK] });
    const { getByLabelText, queryByText } = await render(<MyBooksScreen />);
    await waitFor(() => getByLabelText('Dune — wishlisted'));
    await fireEvent.press(getByLabelText('Dune — wishlisted'));
    await waitFor(() => getByLabelText('Remove Dune'));

    // Deliberately not awaited — see the analogous PATCH test above for why.
    fireEvent.press(getByLabelText('Remove Dune'));

    await waitFor(() => expect(queryByText('Dune')).toBeNull());
    await act(async () => {
      resolveDelete();
    });
  });

  it('restores book when DELETE fails', async () => {
    mockDelete.mockRejectedValue(new Error('Network error'));
    mockGet.mockResolvedValue({ data: [WISHLISTED_BOOK] });
    const { getByLabelText, queryByText } = await render(<MyBooksScreen />);
    await waitFor(() => getByLabelText('Dune — wishlisted'));
    await fireEvent.press(getByLabelText('Dune — wishlisted'));
    await waitFor(() => getByLabelText('Remove Dune'));

    await act(async () => {
      await fireEvent.press(getByLabelText('Remove Dune'));
    });

    await waitFor(() => expect(queryByText('Dune')).toBeTruthy());
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
});

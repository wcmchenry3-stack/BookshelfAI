import React from 'react';
import { act, fireEvent, render, waitFor } from '@testing-library/react-native';

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

jest.mock('../../hooks/useTheme', () => ({
  useTheme: () => ({
    theme: {
      colors: {
        background: '#fff',
        surface: '#f5f5f5',
        border: '#ccc',
        text: '#000',
        textSecondary: '#888',
        primary: '#007AFF',
      },
      typography: { fontSizeBase: 16, fontSizeSM: 14 },
    },
  }),
}));

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

describe('WishlistScreen', () => {
  it('shows loading spinner on mount', async () => {
    mockGet.mockReturnValue(new Promise(() => {})); // never resolves
    const { getByTestId } = await render(<WishlistScreen />);
    expect(getByTestId('loading-spinner')).toBeTruthy();
  });

  it('shows empty state when wishlist is empty', async () => {
    mockGet.mockResolvedValue({ data: [] });
    const { getByText } = await render(<WishlistScreen />);
    await waitFor(() => expect(getByText(/Your wishlist is empty/)).toBeTruthy());
  });

  it('renders book titles and authors when data loaded', async () => {
    mockGet.mockResolvedValue({ data: [BOOK_1, BOOK_2] });
    const { getByText } = await render(<WishlistScreen />);
    await waitFor(
      () => {
        expect(getByText('Dune')).toBeTruthy();
        expect(getByText('Frank Herbert')).toBeTruthy();
        expect(getByText('Foundation')).toBeTruthy();
      },
      { timeout: 10_000 }
    );
  }, 15_000);

  it('renders publish year when edition has one', async () => {
    mockGet.mockResolvedValue({ data: [BOOK_1] });
    const { getByText } = await render(<WishlistScreen />);
    await waitFor(() => expect(getByText('1965')).toBeTruthy());
  });

  it('calls PATCH and removes book when Mark Purchased pressed', async () => {
    mockGet.mockResolvedValue({ data: [BOOK_1] });
    const { getByLabelText, queryByText } = await render(<WishlistScreen />);
    await waitFor(() => expect(getByLabelText('Mark Dune as purchased')).toBeTruthy());

    await act(async () => {
      await fireEvent.press(getByLabelText('Mark Dune as purchased'));
    });

    expect(mockPatch).toHaveBeenCalledWith('/user-books/ub-1', { status: 'purchased' });
    await waitFor(() => expect(queryByText('Dune')).toBeNull());
  });

  it('removes book optimistically before PATCH resolves', async () => {
    mockGet.mockResolvedValue({ data: [BOOK_1] });
    const { getByLabelText, queryByText } = await render(<WishlistScreen />);

    let resolvePatch!: () => void;
    mockPatch.mockReturnValue(
      new Promise((resolve) => {
        resolvePatch = () => resolve({ data: {} });
      })
    );

    await waitFor(() => expect(getByLabelText('Mark Dune as purchased')).toBeTruthy());

    // Deliberately not awaited: under v14, awaiting fireEvent.press blocks
    // until all work the event triggers has settled, which would hang here
    // on the still-pending PATCH call — defeating the point of asserting
    // optimistic (pre-resolution) UI state.
    fireEvent.press(getByLabelText('Mark Dune as purchased'));

    // Item gone before PATCH resolves
    await waitFor(() => expect(queryByText('Dune')).toBeNull());
    await act(async () => {
      resolvePatch();
    });
  });

  it('restores book when PATCH fails', async () => {
    mockPatch.mockRejectedValue(new Error('Network error'));
    mockGet.mockResolvedValue({ data: [BOOK_1] });
    const { getByLabelText, queryByText } = await render(<WishlistScreen />);
    await waitFor(() => expect(getByLabelText('Mark Dune as purchased')).toBeTruthy());

    await act(async () => {
      await fireEvent.press(getByLabelText('Mark Dune as purchased'));
    });

    await waitFor(() => expect(queryByText('Dune')).toBeTruthy());
  });

  it('calls DELETE and removes book when Remove pressed', async () => {
    mockGet.mockResolvedValue({ data: [BOOK_1] });
    const { getByLabelText, queryByText } = await render(<WishlistScreen />);
    await waitFor(() => expect(getByLabelText('Remove Dune from wishlist')).toBeTruthy());

    await act(async () => {
      await fireEvent.press(getByLabelText('Remove Dune from wishlist'));
    });

    expect(mockDelete).toHaveBeenCalledWith('/user-books/ub-1');
    await waitFor(() => expect(queryByText('Dune')).toBeNull());
  });

  it('removes book optimistically before DELETE resolves', async () => {
    mockGet.mockResolvedValue({ data: [BOOK_1] });
    const { getByLabelText, queryByText } = await render(<WishlistScreen />);

    let resolveDelete!: () => void;
    mockDelete.mockReturnValue(
      new Promise((resolve) => {
        resolveDelete = () => resolve({});
      })
    );

    await waitFor(() => expect(getByLabelText('Remove Dune from wishlist')).toBeTruthy());

    // Deliberately not awaited — see the analogous PATCH test above for why.
    fireEvent.press(getByLabelText('Remove Dune from wishlist'));

    await waitFor(() => expect(queryByText('Dune')).toBeNull());
    await act(async () => {
      resolveDelete();
    });
  });

  it('restores book when DELETE fails', async () => {
    mockDelete.mockRejectedValue(new Error('Network error'));
    mockGet.mockResolvedValue({ data: [BOOK_1] });
    const { getByLabelText, queryByText } = await render(<WishlistScreen />);
    await waitFor(() => expect(getByLabelText('Remove Dune from wishlist')).toBeTruthy());

    await act(async () => {
      await fireEvent.press(getByLabelText('Remove Dune from wishlist'));
    });

    await waitFor(() => expect(queryByText('Dune')).toBeTruthy());
  });

  it('fetches with status=wishlisted filter', async () => {
    mockGet.mockResolvedValue({ data: [] });
    await render(<WishlistScreen />);
    await waitFor(() =>
      expect(mockGet).toHaveBeenCalledWith('/user-books', { params: { status: 'wishlisted' } })
    );
  });
});

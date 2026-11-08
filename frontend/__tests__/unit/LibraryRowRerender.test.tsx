import React from 'react';
import { act, fireEvent, render, waitFor } from '@testing-library/react-native';

import MyBooksScreen from '../../app/(tabs)/my-books';
import WishlistScreen from '../../app/(tabs)/wishlist';
import { userBookRow } from '../helpers/screenMocks';

// #929: on the real screens (MyBookRow / WishlistRow), a change to one book
// re-renders only that book's row.

const mockGet = jest.fn();
const mockPatch = jest.fn();

jest.mock('../../lib/api', () => ({
  api: {
    get: (...args: unknown[]) => mockGet(...args),
    patch: (...args: unknown[]) => mockPatch(...args),
    delete: jest.fn(),
  },
}));
jest.mock('expo-router', () => require('../helpers/screenMocks').focusEffectRouterModule());
jest.mock('../../hooks/useTheme', () => require('../helpers/screenMocks').themeModule());
jest.mock('../../components/LoadingSpinner', () =>
  require('../helpers/screenMocks').loadingSpinnerModule()
);

const mockCoverRenders: Record<string, number> = {};
jest.mock('../../components/ui/BookCover', () => {
  const actual = jest.requireActual('../../components/ui/BookCover');
  const ReactLib = require('react');
  return {
    ...actual,
    BookCover: (props: { title: string; size: string }) => {
      if (props.size === 'xs')
        mockCoverRenders[props.title] = (mockCoverRenders[props.title] ?? 0) + 1;
      return ReactLib.createElement(actual.BookCover, props);
    },
  };
});

const TITLES = ['Dune', 'Emma', 'Ulysses', 'Beloved'];
const books = TITLES.map((title, i) =>
  userBookRow({
    id: `ub-${i}`,
    book: { id: `b-${i}`, title, author: 'An Author', cover_url: null, description: null },
  })
);

const rendered = () =>
  Object.entries(mockCoverRenders)
    .filter(([, n]) => n > 0)
    .map(([title]) => title);
const resetCounts = () => {
  for (const k of Object.keys(mockCoverRenders)) mockCoverRenders[k] = 0;
};

beforeEach(() => {
  jest.clearAllMocks();
  for (const k of Object.keys(mockCoverRenders)) delete mockCoverRenders[k];
  mockGet.mockResolvedValue({ data: books });
});

describe('row re-renders (#929)', () => {
  it('My Books: changing one status re-renders only that row', async () => {
    mockPatch.mockResolvedValue({ data: { ...books[0], status: 'purchased' } });
    const { findByLabelText, getByLabelText } = await render(<MyBooksScreen />);
    await findByLabelText('More options for Dune');
    expect(rendered().sort()).toEqual([...TITLES].sort());
    resetCounts();

    await fireEvent.press(getByLabelText('More options for Dune'));
    await act(async () => {
      await fireEvent.press(await findByLabelText('Mark as Purchased'));
    });
    await waitFor(() => expect(getByLabelText('Dune — Purchased, by An Author')).toBeTruthy());
    expect(rendered()).toEqual(['Dune']);
  });

  it('Wishlist: a pending change re-renders only that row', async () => {
    mockPatch.mockReturnValue(new Promise(() => {}));
    const { findByLabelText } = await render(<WishlistScreen />);
    const chip = await findByLabelText('Mark Dune as bought');
    resetCounts();

    fireEvent.press(chip);
    await waitFor(() => expect(chip.props.accessibilityState.disabled).toBe(true));
    expect(rendered()).toEqual(['Dune']);
  });
});

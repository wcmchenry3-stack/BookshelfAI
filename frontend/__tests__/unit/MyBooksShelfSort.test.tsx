import React from 'react';
import { act, fireEvent, render, waitFor } from '@testing-library/react-native';

import MyBooksScreen from '../../app/(tabs)/my-books';
import { userBookRow } from '../helpers/screenMocks';
import { getItem, setItem } from '../../lib/storage';

const mockGet = jest.fn();

jest.mock('../../lib/api', () => ({
  api: { get: (...args: unknown[]) => mockGet(...args), patch: jest.fn(), delete: jest.fn() },
}));
jest.mock('../../lib/storage', () => ({ getItem: jest.fn(), setItem: jest.fn() }));
jest.mock('expo-router', () => require('../helpers/screenMocks').focusEffectRouterModule());
jest.mock('../../hooks/useTheme', () => require('../helpers/screenMocks').themeModule());
jest.mock('../../components/LoadingSpinner', () =>
  require('../helpers/screenMocks').loadingSpinnerModule()
);

function row(id: string, title: string, author: string, status: string) {
  return userBookRow({ id, status, book: { id: `b-${id}`, title, author, cover_url: null } });
}

// Server order: newest first.
const LIBRARY = [
  row('1', 'Moby-Dick', 'Herman Melville', 'reading'),
  row('2', 'Dune', 'Frank Herbert', 'purchased'),
  row('3', 'Emma', 'Jane Austen', 'wishlisted'),
];

const ROW_LABEL = / — .*, by /;

async function renderLibrary(data = LIBRARY) {
  mockGet.mockResolvedValue({ data });
  const screen = await render(<MyBooksScreen />);
  await waitFor(() => screen.getByTestId('my-books-search'));
  return screen;
}

const rowTitles = (screen: Awaited<ReturnType<typeof renderLibrary>>) =>
  screen
    .getAllByLabelText(ROW_LABEL)
    .map((n) => String(n.props.accessibilityLabel).split(' — ')[0]);

beforeEach(() => {
  jest.clearAllMocks();
  (getItem as jest.Mock).mockResolvedValue(null);
  (setItem as jest.Mock).mockResolvedValue(undefined);
});

describe('My Books Reading now shelf (#933)', () => {
  it('shows the shelf on All with no search, and the book stays in All books', async () => {
    const screen = await renderLibrary();
    expect(screen.getByLabelText('Reading now, 1 book')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Moby-Dick, by Herman Melville' })).toBeTruthy();
    expect(screen.getByRole('header', { name: 'All books' })).toBeTruthy();
    expect(rowTitles(screen)).toEqual(['Moby-Dick', 'Dune', 'Emma']);
  });

  it('tapping a shelf book opens its detail sheet', async () => {
    const screen = await renderLibrary();
    await fireEvent.press(screen.getByRole('button', { name: 'Moby-Dick, by Herman Melville' }));
    await waitFor(() => expect(screen.getByLabelText('Close detail')).toBeTruthy());
  });

  it('hides the shelf on another chip and while searching', async () => {
    const screen = await renderLibrary();
    await fireEvent.press(screen.getByLabelText(/^Filter by Purchased/));
    expect(screen.queryByLabelText(/^Reading now/)).toBeNull();
    await fireEvent.press(screen.getByLabelText(/^Filter by All/));
    expect(screen.getByLabelText('Reading now, 1 book')).toBeTruthy();

    await fireEvent.changeText(screen.getByTestId('my-books-search'), 'dune');
    expect(screen.queryByLabelText(/^Reading now/)).toBeNull();
    await fireEvent.changeText(screen.getByTestId('my-books-search'), '');
    expect(screen.getByLabelText('Reading now, 1 book')).toBeTruthy();
  });

  it('has no shelf when no book is being read', async () => {
    const none = await renderLibrary([LIBRARY[1], LIBRARY[2]]);
    expect(none.queryByLabelText(/^Reading now/)).toBeNull();
    expect(none.queryByText('Reading now')).toBeNull();
    expect(none.getByRole('header', { name: 'All books' })).toBeTruthy();
  });

  it('keeps the search field mounted when the shelf appears and disappears', async () => {
    const screen = await renderLibrary();
    const field = screen.getByTestId('my-books-search');
    await fireEvent.changeText(field, 'moby');
    expect(screen.getByTestId('my-books-search')).toBe(field);
    await fireEvent.press(screen.getByLabelText(/^Filter by Reading/));
    expect(screen.getByTestId('my-books-search')).toBe(field);
  });
});

describe('My Books sort (#933)', () => {
  it('defaults to Recently added and says so on the Sort button and the label', async () => {
    const screen = await renderLibrary();
    expect(screen.getByLabelText('Sort, currently Recently added')).toBeTruthy();
    expect(screen.getByTestId('my-books-sort-label').props.children).toBe('Recently added');
  });

  it('sorts by title from the sheet, saves the choice and marks it selected', async () => {
    const screen = await renderLibrary();
    await fireEvent.press(screen.getByTestId('my-books-sort'));
    expect(screen.getByText('Sort by')).toBeTruthy();
    expect(screen.getByTestId('sort-option-added').props.accessibilityState.selected).toBe(true);

    await fireEvent.press(screen.getByTestId('sort-option-title'));
    expect(setItem).toHaveBeenCalledWith('library.sort', 'title');
    expect(rowTitles(screen)).toEqual(['Dune', 'Emma', 'Moby-Dick']);
    expect(screen.getByLabelText('Sort, currently Title A–Z')).toBeTruthy();
    expect(screen.getByTestId('my-books-sort-label').props.children).toBe('Title A–Z');
    // The shelf keeps its own order and is not affected by the sort.
    expect(screen.getByLabelText('Reading now, 1 book')).toBeTruthy();

    await fireEvent.press(screen.getByTestId('my-books-sort'));
    expect(screen.getByTestId('sort-option-title').props.accessibilityState.selected).toBe(true);
    await fireEvent.press(screen.getByTestId('sort-option-author'));
    expect(setItem).toHaveBeenLastCalledWith('library.sort', 'author');
    expect(rowTitles(screen)).toEqual(['Dune', 'Moby-Dick', 'Emma']);
  });

  it('applies the saved sort on mount', async () => {
    (getItem as jest.Mock).mockResolvedValue('author');
    const screen = await renderLibrary();
    await waitFor(() => expect(rowTitles(screen)).toEqual(['Dune', 'Moby-Dick', 'Emma']));
    expect(getItem).toHaveBeenCalledWith('library.sort');
    expect(screen.getByLabelText('Sort, currently Author A–Z')).toBeTruthy();
  });

  it('a choice made before the saved one loads wins over it', async () => {
    let resolve!: (v: string) => void;
    (getItem as jest.Mock).mockReturnValue(new Promise<string>((r) => (resolve = r)));
    const screen = await renderLibrary();
    await fireEvent.press(screen.getByTestId('my-books-sort'));
    await fireEvent.press(screen.getByTestId('sort-option-title'));
    await act(async () => resolve('author'));
    expect(rowTitles(screen)).toEqual(['Dune', 'Emma', 'Moby-Dick']);
  });

  it('ignores an unknown saved value', async () => {
    (getItem as jest.Mock).mockResolvedValue('priority');
    const screen = await renderLibrary();
    expect(rowTitles(screen)).toEqual(['Moby-Dick', 'Dune', 'Emma']);
  });

  it('sorts within the active filter and search', async () => {
    const screen = await renderLibrary();
    await fireEvent.press(screen.getByTestId('my-books-sort'));
    await fireEvent.press(screen.getByTestId('sort-option-title'));
    await fireEvent.changeText(screen.getByTestId('my-books-search'), 'e');
    // "Dune", "Emma" and "Moby-Dick" (Herman Melville) all contain an "e".
    expect(rowTitles(screen)).toEqual(['Dune', 'Emma', 'Moby-Dick']);
    await fireEvent.changeText(screen.getByTestId('my-books-search'), 'austen');
    expect(rowTitles(screen)).toEqual(['Emma']);
  });

  it('memoises the sort: typing nothing new does not rebuild the collator', async () => {
    const spy = jest.spyOn(Intl, 'Collator');
    const screen = await renderLibrary();
    const created = spy.mock.calls.length;
    await fireEvent.press(screen.getByTestId('my-books-sort'));
    await fireEvent.press(screen.getByTestId('sort-option-title'));
    await fireEvent.press(screen.getByTestId('my-books-sort'));
    expect(spy.mock.calls.length).toBe(created);
    spy.mockRestore();
  });

  it('has no Sort button for an empty library', async () => {
    mockGet.mockResolvedValue({ data: [] });
    const screen = await render(<MyBooksScreen />);
    await waitFor(() => screen.getByText('Your library is empty'));
    expect(screen.queryByTestId('my-books-sort')).toBeNull();
  });
});

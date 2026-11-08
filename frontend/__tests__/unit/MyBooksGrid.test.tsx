import React from 'react';
import { Dimensions, StyleSheet } from 'react-native';
import { act, fireEvent, render, waitFor } from '@testing-library/react-native';

import MyBooksScreen from '../../app/(tabs)/my-books';
import { gridCellWidth, gridColumns } from '../../components/library/libraryShared';
import { userBookRow } from '../helpers/screenMocks';
import { getItem, setItem } from '../../lib/storage';

const mockGet = jest.fn();

// Records the props of every FlatList render, to assert numColumns, the key and the render window.
const mockListProps: Record<string, unknown>[] = [];
jest.mock('react-native/Libraries/Lists/FlatList', () => {
  const actual = jest.requireActual('react-native/Libraries/Lists/FlatList');
  const ReactLib = require('react');
  const Actual = actual.default ?? actual;
  const Wrapped = ReactLib.forwardRef((props: Record<string, unknown>, ref: unknown) => {
    mockListProps.push(props);
    ReactLib.useEffect(() => {
      mockListMounts += 1;
    }, []);
    return ReactLib.createElement(Actual, { ...props, ref });
  });
  return { __esModule: true, default: Wrapped };
});
// Each mount of the list instance; a changed `key` remounts it.
let mockListMounts = 0;
const lastListProps = () => mockListProps[mockListProps.length - 1];

jest.mock('../../lib/api', () => ({
  api: { get: (...args: unknown[]) => mockGet(...args), patch: jest.fn(), delete: jest.fn() },
}));
jest.mock('../../lib/storage', () => ({ getItem: jest.fn(), setItem: jest.fn() }));
jest.mock('expo-router', () => require('../helpers/screenMocks').focusEffectRouterModule());
jest.mock('../../hooks/useTheme', () => require('../helpers/screenMocks').themeModule());
jest.mock('../../components/LoadingSpinner', () =>
  require('../helpers/screenMocks').loadingSpinnerModule()
);

// Counts renders of the grid covers (md), which re-render only when their cell does.
const mockCellRenders: Record<string, number> = {};
jest.mock('../../components/ui/BookCover', () => {
  const actual = jest.requireActual('../../components/ui/BookCover');
  const ReactLib = require('react');
  return {
    ...actual,
    BookCover: (props: { title: string; size: string }) => {
      if (props.size === 'md')
        mockCellRenders[props.title] = (mockCellRenders[props.title] ?? 0) + 1;
      return ReactLib.createElement(actual.BookCover, props);
    },
  };
});

function row(id: string, title: string, author: string, status: string, extra = {}) {
  return userBookRow({
    id,
    status,
    book: { id: `b-${id}`, title, author, cover_url: null },
    ...extra,
  });
}

const LIBRARY = [
  row('1', 'Moby-Dick', 'Herman Melville', 'reading'),
  row('2', 'Dune', 'Frank Herbert', 'purchased'),
  row('3', 'Emma', 'Jane Austen', 'wishlisted', { unverified: true }),
];

async function renderLibrary(data: unknown[] = LIBRARY) {
  mockGet.mockResolvedValue({ data });
  const screen = await render(<MyBooksScreen />);
  await waitFor(() => screen.getByTestId('my-books-search'));
  return screen;
}

const setWidth = (width: number) =>
  Dimensions.set({
    window: { width, height: 800, scale: 2, fontScale: 1 },
    screen: { width, height: 800, scale: 2, fontScale: 1 },
  });

beforeEach(() => {
  jest.clearAllMocks();
  mockListProps.length = 0;
  mockListMounts = 0;
  for (const k of Object.keys(mockCellRenders)) delete mockCellRenders[k];
  setWidth(390);
  (getItem as jest.Mock).mockResolvedValue(null);
  (setItem as jest.Mock).mockResolvedValue(undefined);
});

describe('gridColumns (#934)', () => {
  it.each([
    [320, 3],
    [390, 3],
    [768, 6],
    [1024, 8],
  ])('is %i wide -> %i columns', (width, cols) => {
    expect(gridColumns(width)).toBe(cols);
  });
});

describe('gridCellWidth (#934)', () => {
  it.each([
    [320, 85],
    [360, 98],
    [390, 104],
    [768, 104],
    [1024, 104],
  ])('is %i wide -> cell %i', (width, cell) => {
    expect(gridCellWidth(width, gridColumns(width))).toBe(cell);
  });
});

describe('My Books cover grid (#934)', () => {
  it('shows the list by default, with the shelf and row menus', async () => {
    const screen = await renderLibrary();
    expect(screen.getByLabelText('Show as grid')).toBeTruthy();
    expect(screen.queryByTestId('book-grid-cell')).toBeNull();
    expect(screen.getAllByTestId('book-row').length).toBe(3);
    expect(screen.getByLabelText('Reading now, 1 book')).toBeTruthy();
    expect(screen.getByLabelText('More options for Dune')).toBeTruthy();
    expect(screen.getByTestId('my-books-view-toggle')).toBeTruthy();
  });

  it('toggles to the grid, saves the choice, hides the shelf and the row menus', async () => {
    const screen = await renderLibrary();
    await fireEvent.press(screen.getByLabelText('Show as grid'));
    expect(setItem).toHaveBeenCalledWith('library.view', 'grid');
    expect(screen.getByLabelText('Show as list')).toBeTruthy();
    expect(screen.queryByLabelText('Show as grid')).toBeNull();
    expect(screen.getAllByTestId('book-grid-cell').length).toBe(3);
    expect(screen.queryByTestId('book-row')).toBeNull();
    expect(screen.queryByLabelText(/^Reading now/)).toBeNull();
    expect(screen.queryByLabelText(/^More options for/)).toBeNull();
    // Sort stays, and its section header too.
    expect(screen.getByLabelText('Sort, currently Recently added')).toBeTruthy();
    expect(screen.getByTestId('my-books-sort-label')).toBeTruthy();

    await fireEvent.press(screen.getByLabelText('Show as list'));
    expect(setItem).toHaveBeenLastCalledWith('library.view', 'list');
    expect(screen.getAllByTestId('book-row').length).toBe(3);
    expect(screen.getByLabelText('Reading now, 1 book')).toBeTruthy();
  });

  it('applies the saved view on mount', async () => {
    (getItem as jest.Mock).mockImplementation((key: string) =>
      Promise.resolve(key === 'library.view' ? 'grid' : null)
    );
    const screen = await renderLibrary();
    await waitFor(() => expect(screen.getAllByTestId('book-grid-cell').length).toBe(3));
    expect(getItem).toHaveBeenCalledWith('library.view');
  });

  it('ignores an unknown saved view', async () => {
    (getItem as jest.Mock).mockResolvedValue('mosaic');
    const screen = await renderLibrary();
    expect(screen.getAllByTestId('book-row').length).toBe(3);
  });

  it('labels a cell like the row, including Unverified, and opens the detail sheet', async () => {
    const screen = await renderLibrary();
    const rowLabels = screen.getAllByTestId('book-row-body').map((n) => n.props.accessibilityLabel);
    await fireEvent.press(screen.getByLabelText('Show as grid'));
    const cellLabels = screen
      .getAllByTestId('book-grid-cell')
      .map((n) => n.props.accessibilityLabel);
    expect(cellLabels).toEqual(rowLabels);
    expect(cellLabels).toContain('Moby-Dick — Reading, by Herman Melville');
    expect(cellLabels).toContain('Emma — Wishlist, by Jane Austen — Unverified');

    await fireEvent.press(screen.getByLabelText('Dune — Purchased, by Frank Herbert'));
    await waitFor(() => expect(screen.getByLabelText('Close detail')).toBeTruthy());
  });

  it('uses the same search and sort as the list', async () => {
    const screen = await renderLibrary();
    await fireEvent.press(screen.getByLabelText('Show as grid'));
    await fireEvent.press(screen.getByTestId('my-books-sort'));
    await fireEvent.press(screen.getByTestId('sort-option-title'));
    const titles = () =>
      screen
        .getAllByTestId('book-grid-cell')
        .map((n) => String(n.props.accessibilityLabel).split(' — ')[0]);
    expect(titles()).toEqual(['Dune', 'Emma', 'Moby-Dick']);
    await fireEvent.changeText(screen.getByTestId('my-books-search'), 'austen');
    expect(titles()).toEqual(['Emma']);
    await fireEvent.changeText(screen.getByTestId('my-books-search'), 'zzzz');
    expect(screen.getByTestId('my-books-empty-search')).toBeTruthy();
  });

  it('remounts the list when the view or the column count changes', async () => {
    const screen = await renderLibrary();
    expect(lastListProps().numColumns).toBe(1);
    const mounts = [mockListMounts];
    await fireEvent.press(screen.getByLabelText('Show as grid'));
    expect(lastListProps().numColumns).toBe(3);
    mounts.push(mockListMounts);
    await act(async () => setWidth(768));
    await waitFor(() => expect(lastListProps().numColumns).toBe(6));
    mounts.push(mockListMounts);
    await act(async () => setWidth(1024));
    await waitFor(() => expect(lastListProps().numColumns).toBe(8));
    mounts.push(mockListMounts);
    await fireEvent.press(screen.getByLabelText('Show as list'));
    expect(lastListProps().numColumns).toBe(1);
    mounts.push(mockListMounts);
    // list -> grid-3 -> grid-6 -> grid-8 -> list: a new key (a new mount) every time, so
    // numColumns never changes under one key (FlatList would throw). The key is not a prop,
    // so a remount is how a key change shows.
    expect(mounts).toEqual([1, 2, 3, 4, 5]);
    expect(screen.getAllByTestId('book-row').length).toBe(3);
  });

  it('shrinks the cells and scales the cover on a narrow phone, and not on a wide one', async () => {
    setWidth(360);
    const narrow = await renderLibrary();
    await fireEvent.press(narrow.getByLabelText('Show as grid'));
    const cell = narrow.getAllByTestId('book-grid-cell')[0];
    expect(StyleSheet.flatten(cell.props.style).width).toBe(98);
    const wrap = StyleSheet.flatten(narrow.getAllByTestId('book-grid-cover-scaled')[0].props.style);
    expect(wrap).toMatchObject({ width: 98, height: 147, overflow: 'hidden' });
    await narrow.unmount();

    setWidth(390);
    const wide = await renderLibrary();
    await fireEvent.press(wide.getByLabelText('Show as grid'));
    expect(StyleSheet.flatten(wide.getAllByTestId('book-grid-cell')[0].props.style).width).toBe(
      104
    );
    expect(wide.queryByTestId('book-grid-cover-scaled')).toBeNull();
  });

  it('does not re-render an unchanged cell when unrelated screen state changes', async () => {
    const screen = await renderLibrary();
    await fireEvent.press(screen.getByLabelText('Show as grid'));
    expect(mockCellRenders['Dune']).toBeGreaterThan(0);
    const before = { ...mockCellRenders };
    // Sort sheet open and close, and a search that keeps every book (same rows, same keys;
    // a search that drops books changes the row keys and so remounts cells, which is not a re-render).
    await fireEvent.press(screen.getByTestId('my-books-sort'));
    await fireEvent.press(screen.getByText('Close'));
    await fireEvent.changeText(screen.getByTestId('my-books-search'), 'e');
    expect(screen.getAllByTestId('book-grid-cell').length).toBe(3);
    expect(mockCellRenders['Dune']).toBe(before['Dune']);
  });

  it('keeps a toggle made before the saved view loads', async () => {
    let resolve!: (v: string) => void;
    (getItem as jest.Mock).mockImplementation((key: string) =>
      key === 'library.view' ? new Promise<string>((r) => (resolve = r)) : Promise.resolve(null)
    );
    const screen = await renderLibrary();
    await fireEvent.press(screen.getByLabelText('Show as grid'));
    await fireEvent.press(screen.getByLabelText('Show as list'));
    expect(screen.getAllByTestId('book-row').length).toBe(3);
    await act(async () => resolve('grid'));
    expect(screen.getAllByTestId('book-row').length).toBe(3);
    expect(screen.queryByTestId('book-grid-cell')).toBeNull();
    expect(setItem).toHaveBeenLastCalledWith('library.view', 'list');
  });

  it('renders the first screen of 500 books within two batches', async () => {
    const many = Array.from({ length: 500 }, (_, i) =>
      row(String(i), `Book ${i}`, `Author ${i}`, 'purchased')
    );
    const screen = await renderLibrary(many);
    await fireEvent.press(screen.getByLabelText('Show as grid'));
    const { initialNumToRender, maxToRenderPerBatch, numColumns } = lastListProps() as Record<
      string,
      number
    >;
    const FIRST_SCREEN_CELLS = 4 * numColumns; // ~4 rows of 156px covers on an 800px window
    expect(initialNumToRender * numColumns).toBeGreaterThanOrEqual(FIRST_SCREEN_CELLS);
    expect(maxToRenderPerBatch).toBeLessThanOrEqual(10);
    // Only the initial window is mounted, not the 500.
    expect(screen.getAllByTestId('book-grid-cell').length).toBeLessThanOrEqual(
      initialNumToRender * numColumns
    );
  });
});

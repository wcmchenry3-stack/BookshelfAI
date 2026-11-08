import React, { useState } from 'react';
import { StyleSheet, Text, View } from 'react-native';
import { act, fireEvent, render, within } from '@testing-library/react-native';

import {
  LibraryRow,
  LibraryRowSeparator,
  ROW_MIN_HEIGHT,
} from '../../components/library/LibraryRow';
import { ROW_DIVIDER_INSET, type LibraryBook } from '../../components/library/libraryShared';
import { IconButton } from '../../components/ui/IconButton';
import { lightTheme } from '../../theme/light';

jest.mock('../../hooks/useTheme', () => require('../helpers/screenMocks').themeModule());

const mockCoverRenders: Record<string, number> = {};
jest.mock('../../components/ui/BookCover', () => {
  const actual = jest.requireActual('../../components/ui/BookCover');
  const ReactLib = require('react');
  return {
    ...actual,
    BookCover: (props: { title: string }) => {
      mockCoverRenders[props.title] = (mockCoverRenders[props.title] ?? 0) + 1;
      return ReactLib.createElement(actual.BookCover, props);
    },
  };
});

const HIDDEN = { hidden: true };

function makeItem(over: Partial<LibraryBook> & { title?: string; year?: number | null } = {}) {
  const { title = 'Dune', year = 1965, ...rest } = over;
  return {
    id: `ub-${title}`,
    status: 'wishlisted',
    book: { id: `b-${title}`, title, author: 'Frank Herbert', cover_url: null },
    edition: year === null ? null : { publish_year: year },
    ...rest,
  } as LibraryBook;
}

beforeEach(() => {
  for (const k of Object.keys(mockCoverRenders)) delete mockCoverRenders[k];
});

describe('LibraryRow layout (#929)', () => {
  it('is at least 96px tall and lays out cover, text column and trailing slot', async () => {
    const { getByTestId } = await render(
      <LibraryRow item={makeItem()} trailing={<Text>end</Text>} />
    );
    expect(ROW_MIN_HEIGHT).toBe(96);
    expect(StyleSheet.flatten(getByTestId('book-row').props.style).minHeight).toBe(96);
    expect(getByTestId('book-row-trailing')).toBeTruthy();
  });

  it('draws an xs (48x72) decorative cover', async () => {
    const { getByTestId } = await render(<LibraryRow item={makeItem()} onPress={() => {}} />);
    const style = StyleSheet.flatten(getByTestId('book-row-cover', HIDDEN).props.style);
    expect(style.width).toBe(48);
    expect(style.height).toBe(72);
    expect(style.width! / style.height!).toBeCloseTo(2 / 3, 5);
    expect(getByTestId('book-row-cover-wrap', HIDDEN).props['aria-hidden']).toBe(true);
  });

  it.each([
    ['short', 'Up'],
    ['long', 'A Very Long Title That Wraps Over Several Lines Of The Row And More And More'],
  ])('is a fixed 2:3 frame with a %s title, with and without a cover', async (_n, title) => {
    for (const cover of ['https://example.com/c.jpg', null]) {
      const item = makeItem({ title });
      item.book.cover_url = cover;
      const { getByTestId, unmount } = await render(<LibraryRow item={item} />);
      const node = getByTestId('book-row-cover', HIDDEN);
      if (cover) expect(node.props.resizeMode).toBe('cover');
      const style = StyleSheet.flatten(node.props.style);
      expect(style.width! / style.height!).toBeCloseTo(2 / 3, 5);
      expect(style.alignSelf).not.toBe('stretch');
      await unmount();
    }
  });

  it('shows title, author and a one-line title/author', async () => {
    const { getByTestId } = await render(<LibraryRow item={makeItem()} />);
    const text = within(getByTestId('book-row-text'));
    expect(text.getByText('Dune').props.numberOfLines).toBe(1);
    expect(text.getByText('Frank Herbert').props.numberOfLines).toBe(1);
  });

  it.each([
    ['wishlisted', 'statusWishlist', 'Wishlist'],
    ['purchased', 'statusPurchased', 'Purchased'],
    ['reading', 'statusReading', 'Reading'],
    ['read', 'statusRead', 'Read'],
  ] as const)(
    'shows an 8px %s dot and the translated status with the year',
    async (status, role, label) => {
      const { getByTestId, getByText } = await render(<LibraryRow item={makeItem({ status })} />);
      const dot = StyleSheet.flatten(getByTestId('book-row-dot').props.style);
      expect(dot.width).toBe(8);
      expect(dot.height).toBe(8);
      expect(dot.backgroundColor).toBe(lightTheme.colors[role]);
      expect(getByText(`${label} · 1965`)).toBeTruthy();
    }
  );

  it('omits the year when it is unknown', async () => {
    const { getByText } = await render(<LibraryRow item={makeItem({ year: null })} />);
    expect(getByText('Wishlist')).toBeTruthy();
  });

  it('shows only the year, no dot, when showStatus is false', async () => {
    const { getByText, queryByTestId } = await render(
      <LibraryRow item={makeItem()} showStatus={false} />
    );
    expect(getByText('1965')).toBeTruthy();
    expect(queryByTestId('book-row-dot')).toBeNull();
  });

  it('keeps the Unverified badge visible', async () => {
    const { getByText } = await render(<LibraryRow item={makeItem({ unverified: true })} />);
    expect(getByText('Unverified')).toBeTruthy();
  });

  it('draws the divider in the outline colour, inset to the text column', async () => {
    const { getByTestId } = await render(<LibraryRowSeparator />);
    const style = StyleSheet.flatten(getByTestId('book-row-divider').props.style);
    expect(style.backgroundColor).toBe(lightTheme.colors.outline);
    expect(style.marginLeft).toBe(ROW_DIVIDER_INSET);
    expect(ROW_DIVIDER_INSET).toBe(16 + 48 + 14);
  });
});

describe('LibraryRow interaction and accessibility (#907, #929)', () => {
  it('keeps the trailing control a sibling of the body button and does not open on its press', async () => {
    const onOpen = jest.fn();
    const onMore = jest.fn();
    const { getByTestId, getByLabelText } = await render(
      <LibraryRow
        item={makeItem()}
        onPress={onOpen}
        accessibilityLabel="Dune row"
        trailing={<IconButton icon="more-vert" accessibilityLabel="more" onPress={onMore} />}
      />
    );
    const body = getByTestId('book-row-body');
    let node = getByLabelText('more').parent;
    while (node) {
      expect(node).not.toBe(body);
      node = node.parent;
    }
    expect(body.children.length).toBe(0);

    await fireEvent.press(getByLabelText('more'));
    expect(onMore).toHaveBeenCalledTimes(1);
    expect(onOpen).not.toHaveBeenCalled();
    await fireEvent.press(getByLabelText('Dune row'));
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it('gives the trailing IconButton a 44x44 target', async () => {
    const { getByLabelText } = await render(
      <LibraryRow
        item={makeItem()}
        trailing={<IconButton icon="more-vert" accessibilityLabel="more" onPress={() => {}} />}
      />
    );
    const style = StyleSheet.flatten(getByLabelText('more').props.style);
    expect(style.width).toBeGreaterThanOrEqual(44);
    expect(style.height).toBeGreaterThanOrEqual(44);
  });

  it('puts label and hint on the body button', async () => {
    const { getByTestId } = await render(
      <LibraryRow
        item={makeItem()}
        onPress={() => {}}
        accessibilityLabel="Dune — Wishlist, by Frank Herbert"
        accessibilityHint="hint"
      />
    );
    const body = getByTestId('book-row-body');
    expect(body.props.accessibilityLabel).toBe('Dune — Wishlist, by Frank Herbert');
    expect(body.props.accessibilityHint).toBe('hint');
  });

  it('hides the inert cover and text from assistive tech when tappable', async () => {
    const { getByTestId } = await render(<LibraryRow item={makeItem()} onPress={() => {}} />);
    for (const id of ['book-row-text', 'book-row-cover-wrap']) {
      const props = getByTestId(id, HIDDEN).props;
      expect(props.accessibilityElementsHidden).toBe(true);
      expect(props.importantForAccessibility).toBe('no-hide-descendants');
    }
  });

  it('keeps the cover and text visible to assistive tech when not tappable, and has no body button', async () => {
    const { getByTestId, queryByTestId } = await render(<LibraryRow item={makeItem()} />);
    expect(queryByTestId('book-row-body')).toBeNull();
    for (const id of ['book-row-cover-wrap', 'book-row-text']) {
      const props = getByTestId(id, HIDDEN).props;
      expect(props.accessibilityElementsHidden).toBeUndefined();
      expect(props.importantForAccessibility).toBeUndefined();
    }
  });
});

describe('LibraryRow performance (#929)', () => {
  it('re-renders only the row whose item changed (500 rows)', async () => {
    const items = Array.from({ length: 500 }, (_, i) => makeItem({ title: `Book ${i}` }));
    let change!: (id: string) => void;
    const onOpen = jest.fn();
    function List() {
      const [rows, setRows] = useState(items);
      change = (id) =>
        setRows((prev) => prev.map((r) => (r.id === id ? { ...r, status: 'reading' } : r)));
      return (
        <View>
          {rows.map((r) => (
            <LibraryRow key={r.id} item={r} onPress={onOpen} />
          ))}
        </View>
      );
    }
    await render(<List />);
    expect(Object.keys(mockCoverRenders)).toHaveLength(500);
    for (const k of Object.keys(mockCoverRenders)) mockCoverRenders[k] = 0;

    await act(async () => change('ub-Book 7'));
    // The cover is a child of the row: it re-renders only when its row does.
    const touched = Object.entries(mockCoverRenders).filter(([, n]) => n > 0);
    expect(touched.map(([title]) => title)).toEqual(['Book 7']);
  });
});

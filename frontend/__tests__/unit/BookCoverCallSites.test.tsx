import React from 'react';
import { render } from '@testing-library/react-native';

import { BookRow } from '../../components/library/BookRow';
import { BookDetailSheet, type DetailBook } from '../../components/library/BookDetailSheet';
import { BookCandidatePicker } from '../../components/BookCandidatePicker';
import type { LibraryBook } from '../../components/library/libraryShared';

// #902: every place that draws a cover renders BookCover.

const mockCovers: Record<string, unknown>[] = [];
jest.mock('../../components/ui/BookCover', () => {
  const actual = jest.requireActual('../../components/ui/BookCover');
  const ReactLib = require('react');
  return {
    ...actual,
    BookCover: (props: Record<string, unknown>) => {
      mockCovers.push(props);
      return ReactLib.createElement(actual.BookCover, props);
    },
  };
});

jest.mock('../../hooks/useTheme', () => ({
  useTheme: () => ({
    theme: {
      isDark: false,
      colors: {
        background: '#fff',
        surface: '#f5f5f5',
        border: '#ccc',
        text: '#000',
        textSecondary: '#888',
        primary: '#0f426f',
        onPrimary: '#fff',
        secondaryContainer: '#c6e7dd',
        onSecondaryContainer: '#4b6861',
        surfaceContainerLow: '#f5f3ef',
      },
      typography: { fontSizeBase: 16, fontSizeLG: 20, fontSizeSM: 14, fontSizeXS: 12 },
    },
  }),
}));

const ITEM = {
  id: 'ub-1',
  status: 'wishlisted',
  book: {
    id: 'b-1',
    title: 'Dune',
    author: 'Frank Herbert',
    cover_url: 'https://example.com/c.jpg',
    description: null,
  },
  edition: { publish_year: 1965 },
} as unknown as LibraryBook;

beforeEach(() => {
  mockCovers.length = 0;
});

describe('BookCover call sites (#902)', () => {
  it('BookRow renders a decorative BookCover (sm), tappable or not', async () => {
    await render(
      <BookRow item={ITEM} categoryLabel="x">
        <></>
      </BookRow>
    );
    expect(mockCovers[0]).toMatchObject({
      title: 'Dune',
      coverUrl: 'https://example.com/c.jpg',
      size: 'sm',
      decorative: true,
      testID: 'book-row-cover',
    });
    mockCovers.length = 0;
    await render(
      <BookRow item={ITEM} categoryLabel="x" onPress={() => {}}>
        <></>
      </BookRow>
    );
    expect(mockCovers[0]).toMatchObject({ decorative: true });
  });

  it('BookDetailSheet renders BookCover (lg), also without a cover_url', async () => {
    const noCover = { ...ITEM, book: { ...ITEM.book, cover_url: null } } as unknown as DetailBook;
    await render(
      <BookDetailSheet
        selected={noCover}
        actionsDisabled={false}
        onClose={() => {}}
        onAdvance={() => {}}
        onRemove={() => {}}
      />
    );
    expect(mockCovers[0]).toMatchObject({ title: 'Dune', coverUrl: null, size: 'lg' });
  });

  it('BookCandidatePicker renders BookCover (sm) per candidate', async () => {
    await render(
      <BookCandidatePicker
        visible
        candidates={[
          {
            title: 'Dune',
            author: 'Frank Herbert',
            subjects: [],
            confidence: 0.9,
            already_in_library: false,
            editions: [],
            cover_url: 'https://example.com/c.jpg',
          },
        ]}
        onConfirm={() => {}}
        onDismiss={() => {}}
      />
    );
    expect(mockCovers[0]).toMatchObject({
      title: 'Dune',
      coverUrl: 'https://example.com/c.jpg',
      size: 'sm',
    });
  });
});

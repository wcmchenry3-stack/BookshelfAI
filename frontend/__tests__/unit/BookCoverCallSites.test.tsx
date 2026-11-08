import React from 'react';
import { render } from '@testing-library/react-native';

import { LibraryRow } from '../../components/library/LibraryRow';
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

jest.mock('../../hooks/useTheme', () => require('../helpers/screenMocks').themeModule());

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
  it('LibraryRow renders a decorative BookCover (xs), tappable or not', async () => {
    await render(<LibraryRow item={ITEM} />);
    expect(mockCovers[0]).toMatchObject({
      title: 'Dune',
      coverUrl: 'https://example.com/c.jpg',
      size: 'xs',
      decorative: true,
      testID: 'book-row-cover',
    });
    mockCovers.length = 0;
    await render(<LibraryRow item={ITEM} onPress={() => {}} />);
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
    // The detail sheet is the one place that keeps the image label.
    expect(mockCovers[0].decorative).toBeFalsy();
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
      decorative: true,
      testID: 'candidate-cover',
    });
  });
});

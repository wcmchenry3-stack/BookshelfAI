import React from 'react';
import { render } from '@testing-library/react-native';
import i18n from 'i18next';

import { BookGridCell } from '../../components/library/BookGridCell';
import type { LibraryBook } from '../../components/library/libraryShared';

jest.mock('../../hooks/useTheme', () => require('../helpers/screenMocks').themeModule());

const ITEM = {
  id: 'ub-1',
  status: 'reading',
  book: { id: 'b-1', title: 'Dune', author: 'Frank Herbert', cover_url: null },
  edition: null,
} as LibraryBook;

function override(rowStatus: string, statusTab: string) {
  i18n.addResourceBundle(
    'en',
    'my-books',
    { rowStatus: { reading: rowStatus }, statusTab: { reading: statusTab } },
    true,
    true
  );
}

afterEach(() => override('Reading', 'Reading'));

describe('BookGridCell singular status wording (#939)', () => {
  it('shows and speaks my-books:rowStatus, not the chip string (statusTab)', async () => {
    override('Singular reading', 'Chip reading');
    const { getByText, queryByText, getByTestId } = await render(
      <BookGridCell item={ITEM} onOpen={() => {}} />
    );
    expect(getByText('Singular reading')).toBeTruthy();
    expect(queryByText('Chip reading')).toBeNull();
    expect(getByTestId('book-grid-cell').props.accessibilityLabel).toContain('Singular reading');
  });
});

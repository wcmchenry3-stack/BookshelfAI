import { fireEvent, render, waitFor } from '@testing-library/react-native';

import { BookDetailSheet, type DetailBook } from '../../components/library/BookDetailSheet';
import { EmptyState } from '../../components/library/EmptyState';
import { LibraryHeader } from '../../components/library/LibraryHeader';
import {
  NEXT_STATUS,
  STATUS_TAB_KEYS,
  countByStatus,
  filterBooks,
} from '../../components/library/libraryShared';

jest.mock('../../hooks/useTheme', () => require('../helpers/screenMocks').themeModule());

const ITEM: DetailBook = {
  id: 'ub-1',
  status: 'wishlisted',
  book: {
    id: 'b-1',
    title: 'Dune',
    author: 'Frank Herbert',
    cover_url: null,
    description: 'A desert planet epic.',
  },
  edition: { publish_year: 1965, page_count: 604 },
};

describe('libraryShared', () => {
  it('keeps the tab keys and status progression', () => {
    expect(STATUS_TAB_KEYS).toEqual(['all', 'wishlisted', 'purchased', 'reading', 'read']);
    expect(NEXT_STATUS).toEqual({
      wishlisted: 'purchased',
      purchased: 'reading',
      reading: 'read',
    });
  });
});

const row = (status: string, title: string, author: string) => ({
  status,
  book: { title, author },
});
const SHELF = [
  row('reading', 'Emma', 'Jane Austen'),
  row('read', 'Persuasion', 'Jane Austen'),
  row('wishlisted', 'Dune', 'Frank Herbert'),
];

describe('countByStatus / filterBooks', () => {
  it('counts every book under All and each status once', () => {
    expect(countByStatus(SHELF)).toEqual({
      all: 3,
      wishlisted: 1,
      purchased: 0,
      reading: 1,
      read: 1,
    });
  });

  it('filters by status and by a case-insensitive title or author, together', () => {
    expect(filterBooks(SHELF, 'all', '')).toHaveLength(3);
    expect(filterBooks(SHELF, 'reading', '')).toEqual([SHELF[0]]);
    expect(filterBooks(SHELF, 'all', '  AUSTEN ')).toEqual([SHELF[0], SHELF[1]]);
    expect(filterBooks(SHELF, 'read', 'austen')).toEqual([SHELF[1]]);
    expect(filterBooks(SHELF, 'wishlisted', 'austen')).toEqual([]);
    expect(filterBooks(SHELF, 'all', 'dUnE')).toEqual([SHELF[2]]);
  });
});

describe('LibraryHeader', () => {
  it('renders title, subtitle, count, summary label and leading content', async () => {
    const { getByText } = await render(
      <LibraryHeader
        title="My Books"
        subtitle="Sub"
        count={7}
        summaryLabel="in your archive"
        leading={<></>}
      />
    );
    expect(getByText('My Books')).toBeTruthy();
    expect(getByText('Sub')).toBeTruthy();
    expect(getByText('7')).toBeTruthy();
    expect(getByText('in your archive')).toBeTruthy();
  });
});

describe('EmptyState', () => {
  it('renders the message', async () => {
    const { getByText } = await render(<EmptyState message="Nothing here" lineHeight={24} />);
    expect(getByText('Nothing here')).toBeTruthy();
  });
});

describe('BookDetailSheet', () => {
  it('renders nothing visible without a selection', async () => {
    const { queryByText } = await render(
      <BookDetailSheet
        selected={null}
        actionsDisabled={false}
        onClose={() => {}}
        onAdvance={() => {}}
        onRemove={() => {}}
      />
    );
    expect(queryByText('Dune')).toBeNull();
  });

  it('shows details and wires advance, remove and close', async () => {
    const onAdvance = jest.fn();
    const onRemove = jest.fn();
    const onClose = jest.fn();
    const { getByText, getByLabelText } = await render(
      <BookDetailSheet
        selected={ITEM}
        actionsDisabled={false}
        onClose={onClose}
        onAdvance={onAdvance}
        onRemove={onRemove}
      />
    );
    expect(getByText('Dune')).toBeTruthy();
    expect(getByText('A desert planet epic.')).toBeTruthy();
    await fireEvent.press(getByLabelText('Mark as Purchased'));
    expect(onAdvance).toHaveBeenCalledWith(ITEM);
    await fireEvent.press(getByLabelText('Remove Dune'));
    expect(onRemove).toHaveBeenCalledWith(ITEM);
    await fireEvent.press(getByLabelText('Close detail'));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('hides the advance button for a finished book and disables actions on request', async () => {
    const { queryByLabelText, getByLabelText } = await render(
      <BookDetailSheet
        selected={{ ...ITEM, status: 'read' }}
        actionsDisabled
        onClose={() => {}}
        onAdvance={() => {}}
        onRemove={() => {}}
      />
    );
    await waitFor(() =>
      expect(getByLabelText('Remove Dune').props.accessibilityState).toEqual({
        disabled: true,
        busy: false,
      })
    );
    expect(queryByLabelText('Mark as Purchased')).toBeNull();
  });
});

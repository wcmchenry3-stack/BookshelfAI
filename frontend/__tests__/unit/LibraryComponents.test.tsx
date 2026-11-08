import { fireEvent, render, waitFor } from '@testing-library/react-native';

import { BookDetailSheet, type DetailBook } from '../../components/library/BookDetailSheet';
import { EmptyState } from '../../components/library/EmptyState';
import { LibraryHeader } from '../../components/library/LibraryHeader';
import { LibrarySearchBar } from '../../components/library/LibrarySearchBar';
import { StatusTabs } from '../../components/library/StatusTabs';
import { NEXT_STATUS, STATUS_TAB_KEYS } from '../../components/library/libraryShared';

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

describe('StatusTabs', () => {
  it('renders every tab, marks the active one and reports changes', async () => {
    const onChange = jest.fn();
    const { getByText, getAllByRole } = await render(
      <StatusTabs activeTab="reading" onChange={onChange} />
    );
    expect(getAllByRole('button')).toHaveLength(STATUS_TAB_KEYS.length);
    await fireEvent.press(getByText('Purchased'));
    expect(onChange).toHaveBeenCalledWith('purchased');
    const selected = getAllByRole('button').filter((b) => b.props.accessibilityState?.selected);
    expect(selected).toHaveLength(1);
  });
});

describe('LibrarySearchBar', () => {
  it('shows the value and reports typing', async () => {
    const onChangeText = jest.fn();
    const { getByDisplayValue } = await render(
      <LibrarySearchBar value="dun" onChangeText={onChangeText} />
    );
    await fireEvent.changeText(getByDisplayValue('dun'), 'dune');
    expect(onChangeText).toHaveBeenCalledWith('dune');
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

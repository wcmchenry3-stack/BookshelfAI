import { fireEvent, render, waitFor } from '@testing-library/react-native';

import { BookDetailSheet, type DetailBook } from '../../components/library/BookDetailSheet';
import { BookRow, BookRowAction } from '../../components/library/BookRow';
import { EmptyState } from '../../components/library/EmptyState';
import { LibraryHeader } from '../../components/library/LibraryHeader';
import { LibrarySearchBar } from '../../components/library/LibrarySearchBar';
import { StatusTabs } from '../../components/library/StatusTabs';
import { NEXT_STATUS, STATUS_TAB_KEYS } from '../../components/library/libraryShared';

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
        onPrimary: '#ffffff',
        tertiary: '#c9a227',
        onTertiary: '#000000',
        secondary: '#47645d',
        onSurface: '#1b1c1a',
        onSurfaceVariant: '#42474f',
        outline: '#737780',
        secondaryContainer: '#c6e7dd',
        onSecondaryContainer: '#4b6861',
        surfaceContainerLow: '#f5f3ef',
        surfaceContainerHigh: '#eae8e4',
        surfaceContainerHighest: '#e4e2de',
      },
      typography: {
        fontSizeBase: 16,
        fontSizeSM: 14,
        fontSizeLG: 20,
        fontSizeXS: 12,
        fontSizeH1: 32,
        fontFamilyHeadline: 'serif',
      },
    },
  }),
}));

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

describe('BookRow', () => {
  it('renders title, author, year, category label and its actions', async () => {
    const { getByText } = await render(
      <BookRow item={ITEM} categoryLabel="Wishlisted">
        <BookRowAction
          variant="primary"
          label="Do it"
          accessibilityLabel="do it a11y"
          onPress={() => {}}
          disabled={false}
        />
      </BookRow>
    );
    expect(getByText('Dune')).toBeTruthy();
    expect(getByText('Frank Herbert')).toBeTruthy();
    expect(getByText('1965')).toBeTruthy();
    expect(getByText('Wishlisted')).toBeTruthy();
    expect(getByText('Do it')).toBeTruthy();
  });

  it('shows the cover placeholder and the unverified badge', async () => {
    const { getByLabelText, getByText } = await render(
      <BookRow item={{ ...ITEM, unverified: true }} categoryLabel="x">
        {null}
      </BookRow>
    );
    expect(getByLabelText('No cover available')).toBeTruthy();
    expect(getByText('Unverified')).toBeTruthy();
  });

  it('is a pressable card with the given label when onPress is set', async () => {
    const onPress = jest.fn();
    const { getByLabelText } = await render(
      <BookRow item={ITEM} categoryLabel="x" onPress={onPress} accessibilityLabel="card a11y">
        {null}
      </BookRow>
    );
    await fireEvent.press(getByLabelText('card a11y'));
    expect(onPress).toHaveBeenCalledTimes(1);
  });

  it('is a plain card without onPress', async () => {
    const { queryByLabelText } = await render(
      <BookRow item={ITEM} categoryLabel="x" accessibilityLabel="card a11y">
        {null}
      </BookRow>
    );
    expect(queryByLabelText('card a11y')).toBeNull();
  });
});

describe('BookRowAction', () => {
  it('fires onPress when enabled and exposes its a11y label', async () => {
    const onPress = jest.fn();
    const { getByLabelText } = await render(
      <BookRowAction
        variant="secondary"
        label="Remove"
        accessibilityLabel="remove a11y"
        onPress={onPress}
        disabled={false}
      />
    );
    await fireEvent.press(getByLabelText('remove a11y'));
    expect(onPress).toHaveBeenCalledTimes(1);
  });

  it('does not fire when disabled', async () => {
    const onPress = jest.fn();
    const { getByLabelText } = await render(
      <BookRowAction
        variant="primary"
        label="Go"
        accessibilityLabel="go a11y"
        onPress={onPress}
        disabled
      />
    );
    await fireEvent.press(getByLabelText('go a11y'));
    expect(onPress).not.toHaveBeenCalled();
    expect(getByLabelText('go a11y').props.accessibilityState).toEqual({ disabled: true });
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
      expect(getByLabelText('Remove Dune').props.accessibilityState).toEqual({ disabled: true })
    );
    expect(queryByLabelText('Mark as Purchased')).toBeNull();
  });
});

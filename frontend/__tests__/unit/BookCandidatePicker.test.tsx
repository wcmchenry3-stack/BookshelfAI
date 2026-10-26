import React from 'react';
import { fireEvent, render } from '@testing-library/react-native';

import { BookCandidatePicker, EnrichedBook } from '../../components/BookCandidatePicker';

jest.mock('../../hooks/useTheme', () => ({
  useTheme: () => ({
    theme: {
      colors: {
        background: '#fff',
        surface: '#f5f5f5',
        border: '#ccc',
        text: '#000',
        textSecondary: '#888',
        primary: '#007AFF',
        success: '#34C759',
      },
      typography: { fontSizeBase: 16, fontSizeLG: 20, fontSizeSM: 14 },
    },
  }),
}));

const BOOKS: EnrichedBook[] = [
  {
    title: 'Dune',
    author: 'Frank Herbert',
    open_library_work_id: 'OL45804W',
    subjects: ['Science fiction'],
    confidence: 0.97,
    already_in_library: false,
    editions: [{ publish_year: 1965 }],
  },
  {
    title: 'Foundation',
    author: 'Isaac Asimov',
    open_library_work_id: 'OL100W',
    subjects: [],
    confidence: 0.85,
    already_in_library: true,
    editions: [],
  },
];

describe('BookCandidatePicker', () => {
  const onConfirm = jest.fn();
  const onDismiss = jest.fn();

  beforeEach(() => {
    jest.clearAllMocks();
  });

  it('renders nothing meaningful when not visible', async () => {
    const { queryByText } = await render(
      <BookCandidatePicker
        visible={false}
        candidates={BOOKS}
        onConfirm={onConfirm}
        onDismiss={onDismiss}
      />
    );
    expect(queryByText('Dune')).toBeNull();
  });

  it('renders candidate titles when visible', async () => {
    const { getByText } = await render(
      <BookCandidatePicker
        visible={true}
        candidates={BOOKS}
        onConfirm={onConfirm}
        onDismiss={onDismiss}
      />
    );
    expect(getByText('Dune')).toBeTruthy();
    expect(getByText('Foundation')).toBeTruthy();
  });

  it('renders author names', async () => {
    const { getByText } = await render(
      <BookCandidatePicker
        visible={true}
        candidates={BOOKS}
        onConfirm={onConfirm}
        onDismiss={onDismiss}
      />
    );
    expect(getByText('Frank Herbert')).toBeTruthy();
    expect(getByText('Isaac Asimov')).toBeTruthy();
  });

  it('shows publish year when available', async () => {
    const { getByText } = await render(
      <BookCandidatePicker
        visible={true}
        candidates={BOOKS}
        onConfirm={onConfirm}
        onDismiss={onDismiss}
      />
    );
    expect(getByText('1965')).toBeTruthy();
  });

  it('shows Already owned badge for books in library', async () => {
    const { getByText } = await render(
      <BookCandidatePicker
        visible={true}
        candidates={BOOKS}
        onConfirm={onConfirm}
        onDismiss={onDismiss}
      />
    );
    expect(getByText('Already owned')).toBeTruthy();
  });

  it('toggles a book and confirms only the ticked books', async () => {
    const { getByLabelText, getByText } = await render(
      <BookCandidatePicker
        visible={true}
        candidates={BOOKS}
        onConfirm={onConfirm}
        onDismiss={onDismiss}
      />
    );
    await fireEvent.press(getByLabelText('Select Dune by Frank Herbert'));
    await fireEvent.press(getByText('Add 1 book to wishlist'));
    expect(onConfirm).toHaveBeenCalledWith([BOOKS[0]]);
  });

  it('starts with nothing ticked and the add button disabled without preselect', async () => {
    const { getByText } = await render(
      <BookCandidatePicker
        visible={true}
        candidates={BOOKS}
        onConfirm={onConfirm}
        onDismiss={onDismiss}
      />
    );
    await fireEvent.press(getByText('Select books to add'));
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('preselects every book not already in the library', async () => {
    const { getByLabelText, getByText } = await render(
      <BookCandidatePicker
        visible={true}
        candidates={BOOKS}
        onConfirm={onConfirm}
        onDismiss={onDismiss}
        preselect
      />
    );
    expect(getByLabelText('Select Dune by Frank Herbert').props.accessibilityState).toEqual(
      expect.objectContaining({ checked: true })
    );
    expect(getByLabelText('Select Foundation by Isaac Asimov').props.accessibilityState).toEqual(
      expect.objectContaining({ checked: false })
    );
    await fireEvent.press(getByText('Add 1 book to wishlist'));
    expect(onConfirm).toHaveBeenCalledWith([BOOKS[0]]);
  });

  it('select all ticks every book not already owned, then clear all unticks them', async () => {
    const { getByLabelText, getByText } = await render(
      <BookCandidatePicker
        visible={true}
        candidates={BOOKS}
        onConfirm={onConfirm}
        onDismiss={onDismiss}
      />
    );
    await fireEvent.press(getByLabelText('Select all books'));
    // Foundation is already in the library, so it can't be added (#587).
    await fireEvent.press(getByText('Add 1 book to wishlist'));
    expect(onConfirm).toHaveBeenCalledWith([BOOKS[0]]);

    await fireEvent.press(getByLabelText('Clear all selections'));
    expect(getByText('Select books to add')).toBeTruthy();
  });

  it('shows the book count in the title', async () => {
    const { getByText } = await render(
      <BookCandidatePicker
        visible={true}
        candidates={BOOKS}
        onConfirm={onConfirm}
        onDismiss={onDismiss}
      />
    );
    expect(getByText('2 books found')).toBeTruthy();
  });

  it('hides the enhanced scan option when onEnhance is not provided', async () => {
    const { queryByLabelText } = await render(
      <BookCandidatePicker
        visible={true}
        candidates={BOOKS}
        onConfirm={onConfirm}
        onDismiss={onDismiss}
      />
    );
    expect(queryByLabelText('Try enhanced scan')).toBeNull();
  });

  it('offers an enhanced scan with the remaining credit count', async () => {
    const onEnhance = jest.fn();
    const { getByLabelText, getByText } = await render(
      <BookCandidatePicker
        visible={true}
        candidates={BOOKS}
        onConfirm={onConfirm}
        onDismiss={onDismiss}
        onEnhance={onEnhance}
        enhancedScansRemaining={3}
      />
    );
    expect(getByText('3 enhanced scans left')).toBeTruthy();
    await fireEvent.press(getByLabelText('Try enhanced scan'));
    expect(onEnhance).toHaveBeenCalled();
  });

  it('disables the enhanced scan when out of credits', async () => {
    const onEnhance = jest.fn();
    const { getByLabelText, getByText } = await render(
      <BookCandidatePicker
        visible={true}
        candidates={BOOKS}
        onConfirm={onConfirm}
        onDismiss={onDismiss}
        onEnhance={onEnhance}
        enhancedScansRemaining={0}
      />
    );
    expect(getByText('No enhanced scans left')).toBeTruthy();
    await fireEvent.press(getByLabelText('Try enhanced scan'));
    expect(onEnhance).not.toHaveBeenCalled();
  });

  it('calls onDismiss when Cancel is pressed', async () => {
    const { getByLabelText } = await render(
      <BookCandidatePicker
        visible={true}
        candidates={BOOKS}
        onConfirm={onConfirm}
        onDismiss={onDismiss}
      />
    );
    await fireEvent.press(getByLabelText('Close picker'));
    expect(onDismiss).toHaveBeenCalled();
  });

  it('renders cover placeholder when cover_url is absent', async () => {
    const { getAllByLabelText } = await render(
      <BookCandidatePicker
        visible={true}
        candidates={BOOKS}
        onConfirm={onConfirm}
        onDismiss={onDismiss}
      />
    );
    // Both books have no cover_url — expect placeholder views
    expect(getAllByLabelText('No cover available').length).toBe(2);
  });

  it('renders cover image when cover_url is present', async () => {
    const withCover: EnrichedBook[] = [{ ...BOOKS[0], cover_url: 'https://example.com/cover.jpg' }];
    const { getByLabelText } = await render(
      <BookCandidatePicker
        visible={true}
        candidates={withCover}
        onConfirm={onConfirm}
        onDismiss={onDismiss}
      />
    );
    expect(getByLabelText('Cover of Dune')).toBeTruthy();
  });
  describe('edition-level ownership (#587)', () => {
    const owned = (ownership: EnrichedBook['ownership']): EnrichedBook => ({
      ...BOOKS[0],
      already_in_library: ownership !== 'not_owned',
      in_library: ownership !== 'not_owned',
      ownership,
    });

    it.each([
      ['owned_same_edition', 'You already have this book'],
      ['owned_other_edition', 'You already have this book in another edition'],
      ['owned_edition_unknown', 'You already have this book'],
    ] as const)('%s shows "%s"', async (ownership, text) => {
      const { getByText, queryByText } = await render(
        <BookCandidatePicker
          visible={true}
          candidates={[owned(ownership)]}
          onConfirm={onConfirm}
          onDismiss={onDismiss}
        />
      );
      expect(getByText(text)).toBeTruthy();
      expect(queryByText('Already owned')).toBeNull();
    });

    it('shows no note for a book not owned', async () => {
      const { queryByText } = await render(
        <BookCandidatePicker
          visible={true}
          candidates={[owned('not_owned')]}
          onConfirm={onConfirm}
          onDismiss={onDismiss}
        />
      );
      expect(queryByText(/You already have this book/)).toBeNull();
    });

    it.each(['owned_same_edition', 'owned_other_edition', 'owned_edition_unknown'] as const)(
      'a %s book cannot be ticked or added',
      async (ownership) => {
        const { getByLabelText, getByText } = await render(
          <BookCandidatePicker
            visible={true}
            candidates={[owned(ownership)]}
            onConfirm={onConfirm}
            onDismiss={onDismiss}
            preselect
          />
        );
        const card = getByLabelText('Select Dune by Frank Herbert');
        expect(card.props.accessibilityState).toEqual(
          expect.objectContaining({ checked: false, disabled: true })
        );
        await fireEvent.press(card);
        await fireEvent.press(getByText('Select books to add'));
        expect(onConfirm).not.toHaveBeenCalled();
      }
    );

    it('select all skips owned books', async () => {
      const books = [owned('not_owned'), { ...owned('owned_other_edition'), title: 'Emma' }];
      const { getByLabelText, getByText } = await render(
        <BookCandidatePicker
          visible={true}
          candidates={books}
          onConfirm={onConfirm}
          onDismiss={onDismiss}
        />
      );
      await fireEvent.press(getByLabelText('Select all books'));
      await fireEvent.press(getByText('Add 1 book to wishlist'));
      expect(onConfirm).toHaveBeenCalledWith([books[0]]);
    });
  });
});

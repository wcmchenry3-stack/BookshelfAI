import React from 'react';
import { fireEvent, render, waitFor } from '@testing-library/react-native';

import { AddUnverifiedBookSheet } from '../../components/AddUnverifiedBookSheet';
import { BookCandidatePicker, type EnrichedBook } from '../../components/BookCandidatePicker';
import { UnverifiedBadge } from '../../components/UnverifiedBadge';
import { draftFromBook, hasNoCatalogMatch, unverifiedFailure } from '../../lib/unverified';

// #616: "Add it yourself" — a form pre-filled from the scan that saves a book
// the catalog could not find as the user's own unverified entry.

const mockPost = jest.fn();
jest.mock('../../lib/api', () => ({
  api: { post: (...a: unknown[]) => mockPost(...a) },
}));

jest.mock('@expo/vector-icons', () => ({ MaterialIcons: () => null }));

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
        onPrimary: '#fff',
        success: '#34C759',
        error: '#f00',
        onSurface: '#000',
        outline: '#777',
        surfaceContainerHigh: '#eee',
        tertiaryFixed: '#ffdea8',
        onTertiaryFixed: '#271900',
      },
      typography: { fontSizeBase: 16, fontSizeLG: 20, fontSizeSM: 14 },
    },
  }),
}));

function httpError(status: number, detail: unknown) {
  return Object.assign(new Error(`Request failed with status code ${status}`), {
    response: { status, data: { detail } },
  });
}

const NO_MATCH: EnrichedBook = {
  title: 'Pond Notes',
  author: 'A. Local',
  subjects: [],
  confidence: 0.6,
  already_in_library: false,
  editions: [{ isbn_10: '0306406152' }],
};
const MATCHED: EnrichedBook = {
  title: 'Dune',
  author: 'Frank Herbert',
  open_library_work_id: 'OL45804W',
  subjects: [],
  confidence: 0.9,
  already_in_library: false,
  editions: [],
};

beforeEach(() => {
  jest.resetAllMocks();
});

describe('lib/unverified', () => {
  it('pre-fills title, author and the ISBN read (13 first, else 10)', () => {
    expect(draftFromBook(NO_MATCH)).toEqual({
      title: 'Pond Notes',
      author: 'A. Local',
      isbn: '0306406152',
    });
    expect(
      draftFromBook({ ...NO_MATCH, editions: [{ isbn_13: '9780306406157', isbn_10: 'x' }] }).isbn
    ).toBe('9780306406157');
    expect(draftFromBook({ ...NO_MATCH, editions: [] }).isbn).toBe('');
  });

  it('knows a result no catalog matched', () => {
    expect(hasNoCatalogMatch(NO_MATCH)).toBe(true);
    expect(hasNoCatalogMatch(MATCHED)).toBe(false);
    expect(hasNoCatalogMatch({ ...NO_MATCH, google_books_id: 'gb1' })).toBe(false);
  });

  it.each([
    [httpError(409, 'unverified_duplicate'), 'duplicate'],
    [httpError(409, 'unverified_limit_reached'), 'limit'],
    [httpError(409, 'owned_same_edition'), 'failed'],
    [httpError(422, [{ loc: ['body', 'isbn'], msg: 'bad' }]), 'isbn'],
    [httpError(422, [{ loc: ['body', 'title'], msg: 'bad' }]), 'invalid'],
    [httpError(422, 'something'), 'invalid'],
    [httpError(500, 'boom'), 'failed'],
    [new Error('Network Error'), 'failed'],
  ])('classifies a failed save (%#)', (err, expected) => {
    expect(unverifiedFailure(err)).toBe(expected);
  });
});

describe('AddUnverifiedBookSheet', () => {
  const draft = draftFromBook(NO_MATCH);

  async function renderSheet() {
    const onSaved = jest.fn();
    const onDismiss = jest.fn();
    const utils = await render(
      <AddUnverifiedBookSheet visible draft={draft} onSaved={onSaved} onDismiss={onDismiss} />
    );
    return { ...utils, onSaved, onDismiss };
  }

  it('is pre-filled from the scan', async () => {
    const { getByTestId, getByText } = await renderSheet();
    expect(getByTestId('unverified-title-input').props.value).toBe('Pond Notes');
    expect(getByTestId('unverified-author-input').props.value).toBe('A. Local');
    expect(getByTestId('unverified-isbn-input').props.value).toBe('0306406152');
    expect(getByText('Add it yourself')).toBeTruthy();
    expect(getByText(/Only you will see it/)).toBeTruthy();
  });

  it('saves the edited details and reports the new entry', async () => {
    const entry = { id: 'ub-9', unverified: true, book: { id: 'c-1', title: 'Pond', author: 'B' } };
    mockPost.mockResolvedValueOnce({ data: entry });
    const { getByTestId, getByLabelText, onSaved } = await renderSheet();

    await fireEvent.changeText(getByTestId('unverified-title-input'), '  Pond  ');
    await fireEvent.changeText(getByTestId('unverified-author-input'), 'B');
    await fireEvent.changeText(getByTestId('unverified-isbn-input'), ' ');
    await fireEvent.press(getByLabelText('Save'));

    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(entry));
    expect(mockPost).toHaveBeenCalledWith('/wishlist/unverified', {
      title: 'Pond',
      author: 'B',
      isbn: null,
    });
  });

  it('sends the ISBN as typed (the server normalises it)', async () => {
    mockPost.mockResolvedValueOnce({ data: { id: 'x', unverified: true, book: {} } });
    const { getByLabelText } = await renderSheet();
    await fireEvent.press(getByLabelText('Save'));
    await waitFor(() => expect(mockPost).toHaveBeenCalled());
    expect(mockPost.mock.calls[0][1]).toEqual({
      title: 'Pond Notes',
      author: 'A. Local',
      isbn: '0306406152',
    });
  });

  it('requires a title and an author before sending', async () => {
    const { getByTestId, getByLabelText, findByText } = await renderSheet();
    await fireEvent.changeText(getByTestId('unverified-title-input'), '   ');
    await fireEvent.changeText(getByTestId('unverified-author-input'), '');
    await fireEvent.press(getByLabelText('Save'));
    expect(await findByText('Enter a title')).toBeTruthy();
    expect(await findByText('Enter an author')).toBeTruthy();
    expect(mockPost).not.toHaveBeenCalled();
  });

  it.each([
    [httpError(409, 'unverified_duplicate'), "You've already added this book yourself."],
    [
      httpError(409, 'unverified_limit_reached'),
      "You've reached the limit of books you can add yourself.",
    ],
    [
      httpError(422, [{ loc: ['body', 'isbn'], msg: 'bad' }]),
      "This ISBN isn't valid. Fix it or leave it blank.",
    ],
    [httpError(422, [{ loc: ['body', 'title'], msg: 'bad' }]), 'Check the details and try again.'],
    [new Error('Network Error'), "Couldn't save this book. Try again."],
  ])('shows why a save failed (%#)', async (err, message) => {
    mockPost.mockRejectedValueOnce(err);
    const { getByLabelText, findByText, onSaved } = await renderSheet();
    await fireEvent.press(getByLabelText('Save'));
    expect(await findByText(message)).toBeTruthy();
    expect(onSaved).not.toHaveBeenCalled();
  });

  it('cancel closes without saving', async () => {
    const { getByLabelText, onDismiss } = await renderSheet();
    await fireEvent.press(getByLabelText('Close form'));
    expect(onDismiss).toHaveBeenCalled();
    expect(mockPost).not.toHaveBeenCalled();
  });

  it('has accessible, 44 pt controls', async () => {
    const { getByLabelText } = await renderSheet();
    const save = getByLabelText('Save');
    expect(save.props.accessibilityHint).toBe('Adds this book to your wishlist, marked Unverified');
    const close = getByLabelText('Close form');
    const flat = (s: unknown) => Object.assign({}, ...[s].flat(3).filter(Boolean));
    expect(flat(close.props.style).minHeight).toBeGreaterThanOrEqual(44);
    expect(flat(save.props.style).minHeight).toBeGreaterThanOrEqual(44);
  });
});

describe('UnverifiedBadge', () => {
  it('says Unverified, with an explanatory label', async () => {
    const { getByText, getByLabelText } = await render(<UnverifiedBadge />);
    expect(getByText('Unverified')).toBeTruthy();
    expect(getByLabelText('Unverified: added by you, not found in the catalog')).toBeTruthy();
  });
});

describe('BookCandidatePicker "Add it yourself" (#616)', () => {
  it('offers it on a result no catalog matched, and only there', async () => {
    const onAddYourself = jest.fn();
    const { getAllByLabelText, getByText } = await render(
      <BookCandidatePicker
        visible
        candidates={[MATCHED, NO_MATCH]}
        onConfirm={jest.fn()}
        onDismiss={jest.fn()}
        onAddYourself={onAddYourself}
      />
    );
    const buttons = getAllByLabelText(/yourself$/);
    expect(buttons).toHaveLength(1);
    expect(buttons[0].props.accessibilityLabel).toBe('Add Pond Notes yourself');
    expect(getByText('Not in our catalog')).toBeTruthy();
    await fireEvent.press(buttons[0]);
    expect(onAddYourself).toHaveBeenCalledWith(NO_MATCH);
  });

  it('is not offered without a handler', async () => {
    const { queryByText } = await render(
      <BookCandidatePicker
        visible
        candidates={[NO_MATCH]}
        onConfirm={jest.fn()}
        onDismiss={jest.fn()}
      />
    );
    expect(queryByText('Add it yourself')).toBeNull();
  });
});

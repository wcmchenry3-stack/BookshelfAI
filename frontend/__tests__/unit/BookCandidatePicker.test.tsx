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
  const onSelect = jest.fn();
  const onDismiss = jest.fn();

  beforeEach(() => {
    jest.clearAllMocks();
  });

  it('renders nothing meaningful when not visible', async () => {
    const { queryByText } = await render(
      <BookCandidatePicker
        visible={false}
        candidates={BOOKS}
        onSelect={onSelect}
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
        onSelect={onSelect}
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
        onSelect={onSelect}
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
        onSelect={onSelect}
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
        onSelect={onSelect}
        onDismiss={onDismiss}
      />
    );
    expect(getByText('Already owned')).toBeTruthy();
  });

  it('calls onSelect with the correct book when tapped', async () => {
    const { getByLabelText } = await render(
      <BookCandidatePicker
        visible={true}
        candidates={BOOKS}
        onSelect={onSelect}
        onDismiss={onDismiss}
      />
    );
    await fireEvent.press(getByLabelText('Select Dune by Frank Herbert'));
    expect(onSelect).toHaveBeenCalledWith(BOOKS[0]);
  });

  it('calls onDismiss when Cancel is pressed', async () => {
    const { getByLabelText } = await render(
      <BookCandidatePicker
        visible={true}
        candidates={BOOKS}
        onSelect={onSelect}
        onDismiss={onDismiss}
      />
    );
    await fireEvent.press(getByLabelText('Close picker'));
    expect(onDismiss).toHaveBeenCalled();
  });

  it('calls onDismiss when None of these is pressed', async () => {
    const { getByLabelText } = await render(
      <BookCandidatePicker
        visible={true}
        candidates={BOOKS}
        onSelect={onSelect}
        onDismiss={onDismiss}
      />
    );
    await fireEvent.press(getByLabelText('None of these books match'));
    expect(onDismiss).toHaveBeenCalled();
  });

  it('renders cover placeholder when cover_url is absent', async () => {
    const { getAllByLabelText } = await render(
      <BookCandidatePicker
        visible={true}
        candidates={BOOKS}
        onSelect={onSelect}
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
        onSelect={onSelect}
        onDismiss={onDismiss}
      />
    );
    expect(getByLabelText('Cover of Dune')).toBeTruthy();
  });
});

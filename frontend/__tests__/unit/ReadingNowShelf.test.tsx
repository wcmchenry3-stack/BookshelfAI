import { fireEvent, render } from '@testing-library/react-native';

import { ReadingNowShelf } from '../../components/library/ReadingNowShelf';
import { SortSheet } from '../../components/library/SortSheet';
import type { LibraryBook } from '../../components/library/libraryShared';

jest.mock('../../hooks/useTheme', () => require('../helpers/screenMocks').themeModule());

function book(id: string, title: string, author: string): LibraryBook {
  return {
    id,
    status: 'reading',
    book: { id: `b-${id}`, title, author, cover_url: null },
    edition: null,
  };
}

describe('ReadingNowShelf (#933)', () => {
  it('is a list named "Reading now, N books" under a "Reading now" header', async () => {
    const books = [book('1', 'Dune', 'Frank Herbert'), book('2', 'Emma', 'Jane Austen')];
    const { getByRole, getByLabelText } = await render(
      <ReadingNowShelf books={books} onOpen={jest.fn()} />
    );
    expect(getByRole('header', { name: 'Reading now' })).toBeTruthy();
    expect(getByLabelText('Reading now, 2 books')).toBeTruthy();
  });

  it('says "1 book" for a single book', async () => {
    const { getByLabelText } = await render(
      <ReadingNowShelf books={[book('1', 'Dune', 'Frank Herbert')]} onOpen={jest.fn()} />
    );
    expect(getByLabelText('Reading now, 1 book')).toBeTruthy();
  });

  it('each item is a button "{title}, by {author}" that opens that book', async () => {
    const books = [book('1', 'Dune', 'Frank Herbert'), book('2', 'Emma', 'Jane Austen')];
    const onOpen = jest.fn();
    const { getByRole, getByText } = await render(
      <ReadingNowShelf books={books} onOpen={onOpen} />
    );
    await fireEvent.press(getByRole('button', { name: 'Emma, by Jane Austen' }));
    expect(onOpen).toHaveBeenCalledWith(books[1]);
    // The title and author are shown under the cover, one line each.
    expect(getByText('Dune').props.numberOfLines).toBe(1);
    expect(getByText('Frank Herbert').props.numberOfLines).toBe(1);
  });

  it('reads just the title when the author is empty', async () => {
    const { getByRole } = await render(
      <ReadingNowShelf books={[book('1', 'Untitled Notes', '')]} onOpen={jest.fn()} />
    );
    expect(getByRole('button', { name: 'Untitled Notes' })).toBeTruthy();
  });

  it('each target is at least 44px wide (104px cover) and the cover is decorative', async () => {
    const { getByTestId, queryByRole } = await render(
      <ReadingNowShelf books={[book('1', 'Dune', 'Frank Herbert')]} onOpen={jest.fn()} />
    );
    const style = getByTestId('reading-shelf-item-1').props.style;
    const flat = Array.isArray(style) ? Object.assign({}, ...style.flat()) : style;
    expect(flat.width).toBe(104);
    expect(queryByRole('image')).toBeNull();
  });
});

describe('SortSheet (#933)', () => {
  it('lists the options with the current one selected and checked', async () => {
    const { getByText, getByTestId, queryByTestId } = await render(
      <SortSheet visible value="title" onSelect={jest.fn()} onClose={jest.fn()} />
    );
    expect(getByText('Sort by')).toBeTruthy();
    expect(getByTestId('sort-option-title').props.accessibilityState).toMatchObject({
      checked: true,
    });
    expect(getByTestId('sort-option-added').props.accessibilityState).toMatchObject({
      checked: false,
    });
    expect(getByTestId('sort-option-author').props.accessibilityState).toMatchObject({
      checked: false,
    });
    expect(getByTestId('sort-options').props.accessibilityRole).toBe('radiogroup');
    expect(getByTestId('sort-option-title').props.accessibilityRole).toBe('radio');
    expect(getByTestId('sort-option-title-check', { includeHiddenElements: true })).toBeTruthy();
    expect(queryByTestId('sort-option-added-check', { includeHiddenElements: true })).toBeNull();
    expect(getByText('Recently added')).toBeTruthy();
    expect(getByText('Title A–Z')).toBeTruthy();
    expect(getByText('Author A–Z')).toBeTruthy();
  });

  it('reports the tapped option', async () => {
    const onSelect = jest.fn();
    const { getByTestId } = await render(
      <SortSheet visible value="added" onSelect={onSelect} onClose={jest.fn()} />
    );
    await fireEvent.press(getByTestId('sort-option-author'));
    expect(onSelect).toHaveBeenCalledWith('author');
  });
});

import { SORT_KEYS, sortBooks } from '../../components/library/libraryShared';

const b = (id: string, title: string, author: string) => ({ id, book: { title, author } });
const ids = (list: { id: string }[]) => list.map((x) => x.id);
const en = new Intl.Collator('en');

describe('sortBooks (#933)', () => {
  const books = [
    b('1', 'The Hobbit', 'Tolkien'),
    b('2', 'Emma', 'Austen'),
    b('3', 'dune', 'Herbert'),
    b('4', 'Anonymous Tales', ''),
    b('5', 'Émile', 'Rousseau'),
  ];

  it('lists the keys in sheet order', () => {
    expect(SORT_KEYS).toEqual(['added', 'title', 'author']);
  });

  it('added keeps the server order and returns the same array', () => {
    expect(sortBooks(books, 'added', en)).toBe(books);
  });

  it('title sorts ignoring case and accents, and does not skip leading articles', () => {
    // "The Hobbit" sorts under T, not H.
    expect(ids(sortBooks(books, 'title', en))).toEqual(['4', '3', '5', '2', '1']);
  });

  it('author sorts A-Z and puts an empty author last', () => {
    expect(ids(sortBooks(books, 'author', en))).toEqual(['2', '3', '5', '1', '4']);
  });

  it('breaks ties on the other field and keeps the input untouched', () => {
    const tied = [b('a', 'Same', 'Zed'), b('b', 'Same', 'Abe'), b('c', 'Other', 'Zed')];
    expect(ids(sortBooks(tied, 'title', en))).toEqual(['c', 'b', 'a']);
    expect(ids(sortBooks(tied, 'author', en))).toEqual(['b', 'c', 'a']);
    expect(ids(tied)).toEqual(['a', 'b', 'c']);
  });

  it('sorts non-Latin titles with the locale collator', () => {
    const ru = [b('1', 'Идиот', 'Достоевский'), b('2', 'Анна Каренина', 'Толстой')];
    expect(ids(sortBooks(ru, 'title', new Intl.Collator('ru')))).toEqual(['2', '1']);
    const ja = [b('1', 'ん', 'a'), b('2', 'あ', 'b'), b('3', 'か', 'c')];
    expect(ids(sortBooks(ja, 'title', new Intl.Collator('ja')))).toEqual(['2', '3', '1']);
    const mixed = [b('1', 'Zebra', 'a'), b('2', 'Ärger', 'b'), b('3', 'apple', 'c')];
    expect(ids(sortBooks(mixed, 'title', new Intl.Collator('de')))).toEqual(['3', '2', '1']);
  });

  it('handles an empty list', () => {
    expect(sortBooks([], 'title', en)).toEqual([]);
  });
});

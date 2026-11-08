import { filterBooks } from '../../components/library/filterBooks';

const dune = { id: '1', book: { title: 'Dune', author: 'Frank Herbert' } };
const found = { id: '2', book: { title: 'Foundation', author: 'Isaac Asimov' } };
const books = [dune, found];

describe('filterBooks', () => {
  it('returns the same list for an empty or blank query', () => {
    expect(filterBooks(books, '')).toBe(books);
    expect(filterBooks(books, '   ')).toBe(books);
  });

  it('matches the title, ignoring case and surrounding spaces', () => {
    expect(filterBooks(books, '  DUN ')).toEqual([dune]);
  });

  it('matches the author', () => {
    expect(filterBooks(books, 'asimov')).toEqual([found]);
  });

  it('returns nothing when neither matches', () => {
    expect(filterBooks(books, 'zzz')).toEqual([]);
  });

  it('ignores accents in both directions', () => {
    const bronte = { id: '3', book: { title: 'Wuthering Heights', author: 'Emily Brontë' } };
    const plain = { id: '4', book: { title: 'Jane Eyre', author: 'Charlotte Bronte' } };
    expect(filterBooks([bronte, plain], 'bronte')).toEqual([bronte, plain]);
    expect(filterBooks([bronte, plain], 'Brontë')).toEqual([bronte, plain]);
  });

  it('treats NFC and NFD spellings as the same text', () => {
    const nfc = { id: '5', book: { title: 'Caf\u00e9', author: 'A' } };
    const nfd = { id: '6', book: { title: 'Cafe\u0301 noir', author: 'B' } };
    expect(filterBooks([nfc, nfd], 'cafe\u0301')).toEqual([nfc, nfd]);
    expect(filterBooks([nfc, nfd], 'caf\u00e9')).toEqual([nfc, nfd]);
  });
});

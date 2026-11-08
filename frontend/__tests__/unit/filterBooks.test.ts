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
});

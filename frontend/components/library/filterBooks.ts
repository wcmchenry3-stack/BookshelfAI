/** The fields a title/author search reads. */
interface Searchable {
  book: { title: string; author: string };
}

/**
 * Books whose title or author contains `query`, ignoring case and surrounding
 * spaces. An empty query returns `books` itself, so callers keep a stable list.
 */
export function filterBooks<T extends Searchable>(books: T[], query: string): T[] {
  const q = query.trim().toLowerCase();
  if (!q) return books;
  return books.filter(
    (b) => b.book.title.toLowerCase().includes(q) || b.book.author.toLowerCase().includes(q)
  );
}

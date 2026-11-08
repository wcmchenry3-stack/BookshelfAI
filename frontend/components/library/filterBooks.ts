/** The fields a title/author search reads. */
interface Searchable {
  book: { title: string; author: string };
}

/** Lower case, without accents: "Brontë" and "Bronte" (or NFC and NFD forms) compare equal. */
const fold = (text: string) => text.normalize('NFD').replace(/\p{M}/gu, '').toLowerCase();

/**
 * Books whose title or author contains `query`, ignoring case, accents and
 * surrounding spaces. An empty query returns `books` itself, so callers keep a stable list.
 */
export function filterBooks<T extends Searchable>(books: T[], query: string): T[] {
  const q = fold(query.trim());
  if (!q) return books;
  return books.filter((b) => fold(b.book.title).includes(q) || fold(b.book.author).includes(q));
}

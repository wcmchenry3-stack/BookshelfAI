/** Constants and types shared by the My Books and Wishlist screens. */

export type Status = 'all' | 'wishlisted' | 'purchased' | 'reading' | 'read';

export const STATUS_TAB_KEYS: Status[] = ['all', 'wishlisted', 'purchased', 'reading', 'read'];

export const NEXT_STATUS: Record<string, string> = {
  wishlisted: 'purchased',
  purchased: 'reading',
  reading: 'read',
};

export const SCREEN_PADDING = 16;

interface StatusBook {
  status: string;
  book: { title: string; author: string };
}

/** How many books each chip counts: `all` is every book, the rest are per status. */
export function countByStatus(books: readonly StatusBook[]): Record<Status, number> {
  const counts: Record<Status, number> = {
    all: books.length,
    wishlisted: 0,
    purchased: 0,
    reading: 0,
    read: 0,
  };
  for (const b of books) {
    if (b.status in counts && b.status !== 'all') counts[b.status as Status] += 1;
  }
  return counts;
}

/** Books on the given status chip; `all` returns `books` itself. */
export function filterByStatus<T extends { status: string }>(books: T[], status: Status): T[] {
  return status === 'all' ? books : books.filter((b) => b.status === status);
}

/** The fields every library list row needs; screens extend it with their own. */
export interface LibraryBook {
  id: string;
  status: string;
  book: {
    id: string;
    title: string;
    author: string;
    cover_url: string | null;
  };
  /** A book the user added themselves; the catalog could not find it (#616). */
  unverified?: boolean;
  edition: {
    publish_year: number | null;
  } | null;
}

/** The theme colour role of each status; used for the dot in a row's meta line. */
export const STATUS_COLOR_KEY = {
  wishlisted: 'statusWishlist',
  purchased: 'statusPurchased',
  reading: 'statusReading',
  read: 'statusRead',
} as const;

/** Left inset of a row's divider: row padding (16) + xs cover (48) + gap (14). */
export const ROW_DIVIDER_INSET = 16 + 48 + 14;

/**
 * The My Books sort options (#933), in the order the sheet lists them. `added` is
 * the server order (newest first). Keep this list data-driven: #452 adds Priority.
 */
export const SORT_KEYS = ['added', 'title', 'author'] as const;
export type SortKey = (typeof SORT_KEYS)[number];
export const DEFAULT_SORT: SortKey = 'added';

/**
 * Orders books by `key` with the given collator (locale-aware, accents and case
 * handled; leading articles are NOT ignored). `added` returns `books` itself, as
 * the server already sends them newest first. A book with no author sorts last
 * under Author. Ties fall back to the other field, then to the incoming order.
 */
export function sortBooks<T extends { book: { title: string; author: string } }>(
  books: T[],
  key: SortKey,
  collator: Intl.Collator
): T[] {
  if (key === 'added') return books;
  const first = key === 'title' ? 'title' : 'author';
  const second = key === 'title' ? 'author' : 'title';
  return [...books].sort((a, b) => {
    const x = a.book[first];
    const y = b.book[first];
    if (first === 'author' && (!x || !y) && x !== y) return x ? -1 : 1;
    return collator.compare(x, y) || collator.compare(a.book[second], b.book[second]);
  });
}

/** Translator shape the label helper needs (a react-i18next `t` for `my-books`). */
type Translate = (key: string, options?: Record<string, unknown>) => string;

/**
 * The spoken label of a My Books book: "Title — Status, by Author", plus
 * "— Unverified" for a book the user added themselves (#616). The list row and
 * the grid cell (#934) share it, so both read the same.
 */
export function bookA11yLabel(
  t: Translate,
  item: { status: string; unverified?: boolean; book: { title: string; author: string } }
): string {
  return t(item.unverified ? 'bookCardUnverifiedA11y' : 'bookCardA11y', {
    title: item.book.title,
    status: t(`statusTab.${item.status}`),
    author: item.book.author,
  });
}

/** Cover grid geometry (#934): `md` cover width, gap between columns, gap between rows. */
export const GRID_COVER_WIDTH = 104;
export const GRID_COLUMN_GAP = 16;
export const GRID_ROW_GAP = 20;

/** Columns of the cover grid: at least 3, more as the window widens. */
export function gridColumns(width: number): number {
  return Math.max(
    3,
    Math.floor(
      (width - 2 * SCREEN_PADDING + GRID_COLUMN_GAP) / (GRID_COVER_WIDTH + GRID_COLUMN_GAP)
    )
  );
}

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

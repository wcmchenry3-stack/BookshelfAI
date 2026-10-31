/** Constants and types shared by the My Books and Wishlist screens. */

export type Status = 'all' | 'wishlisted' | 'purchased' | 'reading' | 'read';

export const STATUS_TAB_KEYS: Status[] = ['all', 'wishlisted', 'purchased', 'reading', 'read'];

export const NEXT_STATUS: Record<string, string> = {
  wishlisted: 'purchased',
  purchased: 'reading',
  reading: 'read',
};

export const SCREEN_PADDING = 16;

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

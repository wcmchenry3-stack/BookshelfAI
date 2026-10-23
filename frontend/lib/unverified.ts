/**
 * Unverified entries (#616): books the catalog (Open Library / Google Books)
 * could not find, which the user adds themselves with
 * POST /wishlist/unverified. They are private to the user and listed with
 * `unverified: true`.
 */

/** The form's starting values, from what the scan read. */
export interface UnverifiedDraft {
  title: string;
  author: string;
  isbn: string;
}

interface ScanReading {
  title: string;
  author: string;
  open_library_work_id?: string | null;
  google_books_id?: string | null;
  editions?: { isbn_13?: string | null; isbn_10?: string | null }[];
}

/** Field limits, the same as the server's (title and author 500, ISBN 20). */
export const UNVERIFIED_LIMITS = { title: 500, author: 500, isbn: 20 } as const;

/**
 * Whether no catalog matched this scan result: it has neither an Open Library
 * work ID nor a Google Books ID, so POST /wishlist refuses it (422
 * `book_not_in_catalog`) and only "Add it yourself" can save it.
 */
export function hasNoCatalogMatch(book: ScanReading): boolean {
  return !book.open_library_work_id && !book.google_books_id;
}

/** The form pre-filled from a scan result: its title, author and ISBN, if read. */
export function draftFromBook(book: ScanReading): UnverifiedDraft {
  const edition = book.editions?.[0];
  return {
    title: (book.title ?? '').slice(0, UNVERIFIED_LIMITS.title),
    author: (book.author ?? '').slice(0, UNVERIFIED_LIMITS.author),
    isbn: (edition?.isbn_13 ?? edition?.isbn_10 ?? '').slice(0, UNVERIFIED_LIMITS.isbn),
  };
}

/** Why POST /wishlist/unverified failed, as the form shows it. */
export type UnverifiedFailure = 'duplicate' | 'limit' | 'isbn' | 'invalid' | 'failed';

/** Map a failed POST /wishlist/unverified to the message the form shows. */
export function unverifiedFailure(reason: unknown): UnverifiedFailure {
  const response = (reason as { response?: { status?: number; data?: { detail?: unknown } } })
    ?.response;
  const detail = response?.data?.detail;
  if (response?.status === 409) {
    if (detail === 'unverified_duplicate') return 'duplicate';
    if (detail === 'unverified_limit_reached') return 'limit';
    return 'failed';
  }
  if (response?.status === 422) {
    // FastAPI validation errors: a list of { loc: ['body', field], ... }.
    const fields = Array.isArray(detail)
      ? detail.map((d) => (d as { loc?: unknown[] })?.loc?.[1])
      : [];
    return fields.includes('isbn') ? 'isbn' : 'invalid';
  }
  return 'failed';
}

/**
 * Unverified entries (#616): books the catalog (Open Library / Google Books)
 * could not find, which the user adds themselves with
 * POST /wishlist/unverified. They are private to the user and listed with
 * `unverified: true`.
 */

/**
 * Which scan candidate a form was pre-filled from (#631): the scan record's
 * id and the candidate's index, as POST /scan returned them. Sent with the
 * save so the server can tell whether the user corrected the reading; never
 * the pre-filled values themselves.
 */
export interface ScanReference {
  scan_record_id: string;
  candidate_index: number;
}

/** The form's starting values, from what the scan read. */
export interface UnverifiedDraft {
  title: string;
  author: string;
  isbn: string;
  /** Set when the draft came from a photo scan's result (#631). */
  scanReference?: ScanReference;
}

interface ScanReading {
  title: string;
  author: string;
  open_library_work_id?: string | null;
  google_books_id?: string | null;
  editions?: { isbn_13?: string | null; isbn_10?: string | null }[];
  /** Which of the scan's candidates this result came from (#631). */
  candidate_index?: number | null;
  /** The scan record it came from, kept by the app with each result (#631). */
  scan_record_id?: string | null;
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

/** The book's scan reference (#631), or undefined when it has none. */
export function scanReferenceOf(book: ScanReading): ScanReference | undefined {
  const id = book.scan_record_id;
  const index = book.candidate_index;
  if (typeof id !== 'string' || !id) return undefined;
  if (typeof index !== 'number' || !Number.isInteger(index) || index < 0) return undefined;
  return { scan_record_id: id, candidate_index: index };
}

/**
 * Keep the scan record's id with each result of a POST /scan response
 * (#631), so it travels with the book into the job (saved with queued and
 * retried jobs) and into an "Add it yourself" draft. Books without a
 * candidate index, or a response without a record id, are left as they are.
 */
export function withScanRecord<T extends ScanReading>(
  books: T[],
  scanRecordId: string | null | undefined
): T[] {
  if (typeof scanRecordId !== 'string' || !scanRecordId) return books;
  return books.map((b) =>
    typeof b.candidate_index === 'number' ? { ...b, scan_record_id: scanRecordId } : b
  );
}

/**
 * The form pre-filled from a scan result: its title, author and ISBN, if
 * read, and (#631) which scan candidate it came from, when known.
 */
export function draftFromBook(book: ScanReading): UnverifiedDraft {
  const edition = book.editions?.[0];
  const scanReference = scanReferenceOf(book);
  return {
    title: (book.title ?? '').slice(0, UNVERIFIED_LIMITS.title),
    author: (book.author ?? '').slice(0, UNVERIFIED_LIMITS.author),
    isbn: (edition?.isbn_13 ?? edition?.isbn_10 ?? '').slice(0, UNVERIFIED_LIMITS.isbn),
    ...(scanReference ? { scanReference } : {}),
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

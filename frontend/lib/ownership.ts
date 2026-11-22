/**
 * Edition-level library ownership (#587), as the server reports it on scan
 * and search results (`ownership`) and in POST /wishlist's 409 `detail`.
 */
export type Ownership =
  'owned_same_edition' | 'owned_other_edition' | 'owned_edition_unknown' | 'not_owned';

/** The 409 `detail` codes POST /wishlist returns for a book the user has. */
const OWNED_CODES: readonly Ownership[] = [
  'owned_same_edition',
  'owned_other_edition',
  'owned_edition_unknown',
];

interface OwnershipFields {
  ownership?: Ownership;
  in_library?: boolean;
  already_in_library?: boolean;
}

/**
 * Whether the user already has this book. Falls back to the work-level
 * flags for results saved before the server sent `ownership`.
 */
export function isOwned(book: OwnershipFields): boolean {
  if (book.ownership) return book.ownership !== 'not_owned';
  return (book.in_library ?? book.already_in_library) === true;
}

/** Whether a failed POST /wishlist means "already in your library". */
export function isAlreadyOwnedError(reason: unknown): boolean {
  const response = (reason as { response?: { status?: number; data?: { detail?: unknown } } })
    ?.response;
  return response?.status === 409 && OWNED_CODES.includes(response.data?.detail as Ownership);
}

/**
 * The 422 `detail` codes POST /wishlist returns for a book the server will not
 * add to the shared catalog (#612): `book_not_in_catalog` (no Open Library /
 * Google Books match) and `catalog_token_invalid` (a stale or expired result).
 * Retrying cannot help either one.
 */
export type CatalogRefusal = 'book_not_in_catalog' | 'catalog_token_invalid';

/** Which catalog refusal a failed POST /wishlist is, or null for any other failure. */
export function catalogRefusal(reason: unknown): CatalogRefusal | null {
  const response = (reason as { response?: { status?: number; data?: { detail?: unknown } } })
    ?.response;
  if (response?.status !== 422) return null;
  const detail = response.data?.detail;
  return detail === 'book_not_in_catalog' || detail === 'catalog_token_invalid' ? detail : null;
}

/**
 * Request timeouts (#766). Kept apart from lib/api.ts so tests that mock the
 * client keep the real values.
 *
 * Only two endpoints do outbound provider work (backend/app/api: books.py and
 * scan.py); everything else is database work and answers in well under a
 * second. A client timeout shorter than the server's own bound makes the app
 * give up on work the server is still doing, so these are derived from the
 * server numbers, not guessed.
 */

/** Default for ordinary (database-only) endpoints: well above their worst case. */
export const DEFAULT_TIMEOUT_MS = 30_000;

/**
 * GET /books/search: Google Books (httpx timeout 10 s, services/google_books.py)
 * then enrichment (ENRICHMENT_DEADLINE_S = 20 s, services/enrichment/service.py) is
 * about 30 s worst case; 45 s leaves a 15 s margin for the network.
 */
export const SEARCH_TIMEOUT_MS = 45_000;

/**
 * POST /scan: the AI call (`scan_timeout_seconds`, default 45 s, allowed up to
 * TIMEOUT_S_BOUNDS = 120 s in app/ai/models_allowlist.py) + enrichment (20 s)
 * = 140 s, plus a photo upload over a slow mobile link: 180 s.
 */
export const SCAN_UPLOAD_TIMEOUT_MS = 180_000;

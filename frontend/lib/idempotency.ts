import * as Crypto from 'expo-crypto';

import { toApiError } from './apiError';

/**
 * Idempotency-Key on library and account writes (#685). `lib/api.ts` adds a
 * fresh key to every write the server honours one on (one call of
 * `api.post` / `patch` / `delete` = one user action) and keeps it on the
 * request config, so its own retries send the same key and the server
 * replays the first response instead of applying the write twice. A caller
 * that retries an action itself (a "Try again" button) can reuse a key by
 * passing the header explicitly.
 */
export const IDEMPOTENCY_KEY_HEADER = 'Idempotency-Key';

/** 409 detail while the first request with the key is still running. */
const IDEMPOTENCY_IN_PROGRESS = 'idempotency_in_progress';
/**
 * 409 detail when the first request with the key committed its write but
 * the server stored no response (it failed right after the commit, or the
 * server stopped). The write was applied: never retry it, show no error,
 * re-read the data instead.
 */
const IDEMPOTENCY_OUTCOME_UNKNOWN = 'idempotency_outcome_unknown';

const KEYED_METHODS: readonly string[] = ['post', 'patch', 'put', 'delete'];

// The writes the server keys (backend `tests/security/test_idempotency_coverage.py`,
// KEYED). /scan is not one: the scan queue owns its retries.
const KEYED_PATHS: readonly RegExp[] = [
  /^\/wishlist(\/unverified)?$/,
  /^\/purchased$/,
  /^\/user-books\/[^/]+(\/restore)?$/,
  /^\/users\/me\/(preferences|ai-consent)$/,
  /^\/account\/export$/,
];

/** Whether the server honours an Idempotency-Key on `method` `path` (a path without query). */
export function isKeyedWrite(method: string | undefined, path: string): boolean {
  if (!KEYED_METHODS.includes(String(method ?? 'get').toLowerCase())) return false;
  return path !== '/user-books/trash' && KEYED_PATHS.some((p) => p.test(path));
}

/** A new key: a random UUID, one per user action. */
export function newIdempotencyKey(): string {
  return Crypto.randomUUID();
}

type HeaderBag = { get?: (name: string) => unknown } & Record<string, unknown>;

/** The key a request config carries, if any. */
export function idempotencyKeyOf(headers: unknown): string | undefined {
  if (!headers || typeof headers !== 'object') return undefined;
  const bag = headers as HeaderBag;
  const value =
    typeof bag.get === 'function'
      ? bag.get(IDEMPOTENCY_KEY_HEADER)
      : (bag[IDEMPOTENCY_KEY_HEADER] ?? bag[IDEMPOTENCY_KEY_HEADER.toLowerCase()]);
  return typeof value === 'string' && value ? value : undefined;
}

/** A `409 idempotency_in_progress`: the first attempt is still running; wait and retry. */
export function isIdempotencyInProgress(status: number | undefined, code: string | undefined) {
  return status === 409 && code === IDEMPOTENCY_IN_PROGRESS;
}

/**
 * True for `409 idempotency_outcome_unknown`: the write already happened.
 * Callers treat it as applied (refresh what they show, no error banner) and
 * `lib/api.ts` never retries it.
 */
export function isIdempotencyOutcomeUnknown(err: unknown): boolean {
  const { status, code } = toApiError(err);
  return status === 409 && code === IDEMPOTENCY_OUTCOME_UNKNOWN;
}

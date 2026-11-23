/**
 * Optimistic concurrency for library entries (#685). Every entry the API
 * returns carries `version`; a write sends it back as `If-Match: "<version>"`
 * and the server answers 412 `version_mismatch` when the entry changed since
 * (on another device). See docs/api.md, "Entry versions".
 */
import type { AxiosRequestConfig } from 'axios';

import { toApiError } from './apiError';

/** 412 detail code: the entry changed since the client read it. */
export const VERSION_MISMATCH = 'version_mismatch';

/**
 * The request config that sends `version` as `If-Match`, as a spreadable
 * argument list: empty when the version is unknown (the server then accepts
 * the write unchecked).
 */
export function ifMatch(version: number | undefined): [] | [AxiosRequestConfig] {
  if (typeof version !== 'number' || !Number.isInteger(version)) return [];
  return [{ headers: { 'If-Match': `"${version}"` } }];
}

/** True for the 412 the server sends when the entry changed elsewhere. */
export function isVersionConflict(err: unknown): boolean {
  const e = toApiError(err);
  return e.status === 412 && e.code === VERSION_MISMATCH;
}

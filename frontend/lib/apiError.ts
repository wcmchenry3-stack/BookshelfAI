/**
 * A typed view of a failed API call (#766). `toApiError(err)` turns whatever
 * an axios call rejected with into one shape, so callers stop poking at
 * `err.response.data.detail` and `err.response.headers['retry-after']`.
 *
 * Backend error shapes it understands (see docs/api.md):
 *  - `{ "detail": "daily_scan_limit" }`            -> code = the string
 *  - `{ "detail": { "code": "x", ... } }`           -> code = detail.code
 *  - `{ "detail": { "detail": { "code": "x" } } }`  -> code = detail.detail.code
 *  - FastAPI validation errors (`detail` is a list) and anything else: no code.
 *
 * It never carries headers, tokens or bodies, so it is safe to log.
 */

/** Longest wait a `Retry-After` is trusted for by the retry layer in api.ts. */
export const MAX_RETRY_AFTER_MS = 30_000;

export interface ApiError {
  /** HTTP status, absent when there was no response. */
  status?: number;
  /** Machine code from the response `detail`, when the server sent one. */
  code?: string;
  /** Parsed `Retry-After`, in ms, uncapped (callers decide how long to wait). */
  retryAfterMs?: number;
  /** No response at all: offline, DNS, connection refused, timeout, CORS block. */
  isNetworkError: boolean;
  /** The request timed out (a subset of `isNetworkError`). */
  isTimeout: boolean;
  /** For logs only; never show it to the user (not translated). */
  message: string;
}

type ErrorLike = {
  code?: unknown;
  message?: unknown;
  response?: { status?: unknown; data?: unknown; headers?: unknown } | null;
};

function readHeader(headers: unknown, name: string): string | undefined {
  if (!headers || typeof headers !== 'object') return undefined;
  const h = headers as { get?: (n: string) => unknown } & Record<string, unknown>;
  let value: unknown;
  if (typeof h.get === 'function') {
    try {
      value = h.get(name);
    } catch {
      value = undefined;
    }
  }
  if (value === undefined || value === null) {
    const key = Object.keys(h).find((k) => k.toLowerCase() === name.toLowerCase());
    value = key ? h[key] : undefined;
  }
  if (Array.isArray(value)) value = value[0];
  return typeof value === 'string' || typeof value === 'number' ? String(value) : undefined;
}

/**
 * `Retry-After` is delta-seconds or an HTTP-date. Returns ms (>= 0), or
 * undefined when absent or unparseable. `now` is injectable for tests.
 */
function parseRetryAfterMs(
  value: string | undefined,
  now: number = Date.now()
): number | undefined {
  if (value === undefined) return undefined;
  const trimmed = value.trim();
  if (trimmed === '') return undefined;
  if (/^\d+(\.\d+)?$/.test(trimmed)) return Math.round(Number(trimmed) * 1000);
  const at = Date.parse(trimmed);
  if (Number.isNaN(at)) return undefined;
  return Math.max(0, at - now);
}

function codeOf(data: unknown): string | undefined {
  if (!data || typeof data !== 'object') return undefined;
  const detail = (data as { detail?: unknown }).detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    const d = detail as { code?: unknown; detail?: unknown };
    if (typeof d.code === 'string') return d.code;
    const inner = d.detail as { code?: unknown } | null | undefined;
    if (inner && typeof inner === 'object' && typeof inner.code === 'string') return inner.code;
  }
  return undefined;
}

/** True for an axios timeout (`ECONNABORTED` / `ETIMEDOUT`) with no response. */
export function isTimeoutError(err: unknown): boolean {
  const e = err as ErrorLike | null;
  if (!e || typeof e !== 'object' || e.response) return false;
  return e.code === 'ECONNABORTED' || e.code === 'ETIMEDOUT';
}

/** Normalise any rejection into an `ApiError`. Never throws. */
export function toApiError(err: unknown): ApiError {
  const e = (err && typeof err === 'object' ? err : {}) as ErrorLike;
  const message = typeof e.message === 'string' ? e.message : 'Unknown error';
  const response = e.response ?? undefined;
  if (!response) {
    return { isNetworkError: true, isTimeout: isTimeoutError(err), message };
  }
  return {
    status: typeof response.status === 'number' ? response.status : undefined,
    code: codeOf(response.data),
    retryAfterMs: parseRetryAfterMs(readHeader(response.headers, 'retry-after')),
    isNetworkError: false,
    isTimeout: false,
    message,
  };
}

/**
 * Central Sentry reporting for unexpected API failures (#724).
 *
 * Before this, only the scan path captured errors, and the event said just
 * "Request failed with status code 403" (no path, no `detail`), so a
 * `403 forbidden` origin lockdown looked like `enhanced_requires_premium`.
 *
 * Rules:
 *  - Report a failure at most once per (method, path template, status, detail)
 *    per app session (module-level Set), so Sentry volume is bounded by the
 *    number of distinct endpoints x codes, not by traffic or retries.
 *  - Only 403 and 5xx are "unexpected". Network errors (no response, #701),
 *    401 (refresh flow), 402/409/429 and the other 4xx the screens handle are
 *    not reported, nor are handled business codes (HANDLED_DETAILS).
 *  - POST /scan is skipped: ScanJobContext already captures it, carrying the
 *    same tags via `apiErrorTags`.
 *  - A fresh sanitized Error is captured, never the AxiosError: that carries
 *    `config.headers.Authorization` and request/response bodies.
 */
import { Sentry } from './sentry';

/** 403 detail the edge/origin lockdown sends (backend/app/core/origin_auth.py). */
export const FORBIDDEN = 'forbidden';

/** Short machine codes safe to send as the `api.detail` tag. */
const KNOWN_DETAILS: ReadonlySet<string> = new Set([
  FORBIDDEN,
  'token_revoked',
  'enhanced_requires_premium',
  'daily_scan_limit',
  'scan_requests_daily_limit',
  'scan_in_progress',
  'enhanced_throttled',
  'scan_disabled',
  'enhanced_scan_disabled',
  'scan_capacity',
  'ai_budget_reached',
  'ai_unavailable',
  'scan_unavailable',
  'account_conflict',
  'catalog_token_invalid',
  'book_not_in_catalog',
  'unverified_duplicate',
  'unverified_limit_reached',
]);

/** Codes the app handles on purpose; never reported. */
const HANDLED_DETAILS: ReadonlySet<string> = new Set([
  'enhanced_requires_premium',
  'daily_scan_limit',
  'scan_requests_daily_limit',
  'scan_in_progress',
  'enhanced_throttled',
  'scan_disabled',
  'enhanced_scan_disabled',
  'scan_capacity',
  'ai_budget_reached',
  'ai_unavailable',
  'scan_unavailable',
]);

type ApiErrorLike = {
  config?: { method?: string; url?: string };
  response?: { status?: number; data?: { detail?: unknown } | null };
};

const reported = new Set<string>();

/** Test helper: forget what was reported this session. */
export function resetApiErrorReporting(): void {
  reported.clear();
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** `/user-books/<uuid>?x=1` -> `/user-books/:id`. No origin, query or IDs. */
export function pathTemplate(url: string | undefined): string {
  if (!url) return 'unknown';
  const noOrigin = url.replace(/^[a-z][a-z0-9+.-]*:\/\/[^/]+/i, '');
  const path = noOrigin.split(/[?#]/)[0] || '/';
  return path
    .split('/')
    .map((seg) => (/^\d+$/.test(seg) || UUID_RE.test(seg) ? ':id' : seg))
    .join('/');
}

function statusOf(err: unknown): number | undefined {
  return (err as ApiErrorLike | null)?.response?.status;
}

/** The response `detail` when it is a known short machine code, else undefined. */
export function knownDetail(err: unknown): string | undefined {
  const d = (err as ApiErrorLike | null)?.response?.data?.detail;
  return typeof d === 'string' && KNOWN_DETAILS.has(d) ? d : undefined;
}

/** True for `403 {"detail":"forbidden"}`: the service/origin refused, not an auth failure. */
export function isServiceUnavailable(err: unknown): boolean {
  return statusOf(err) === 403 && knownDetail(err) === FORBIDDEN;
}

/** Sentry tags describing a failed API call. Never includes headers, tokens or bodies. */
export function apiErrorTags(err: unknown): Record<string, string> {
  const status = statusOf(err);
  const tags: Record<string, string> = {
    'api.path': pathTemplate((err as ApiErrorLike | null)?.config?.url),
  };
  if (status !== undefined) tags['http.status'] = String(status);
  const detail = knownDetail(err);
  if (detail) tags['api.detail'] = detail;
  return tags;
}

function isUnexpected(err: unknown): boolean {
  const status = statusOf(err);
  if (status === undefined) return false; // network error / timeout (#701)
  if (status !== 403 && status < 500) return false; // 401, 402, 409, 429, other handled 4xx
  const detail = knownDetail(err);
  return !(detail && HANDLED_DETAILS.has(detail));
}

/** Report an unexpected API failure once per session. Never throws. */
export function reportApiError(err: unknown): void {
  try {
    if (!isUnexpected(err)) return;
    const tags = apiErrorTags(err);
    const path = tags['api.path'];
    if (path === '/scan') return; // captured by ScanJobContext with the same tags
    const method = ((err as ApiErrorLike).config?.method ?? 'get').toUpperCase();
    const detail = tags['api.detail'] ?? '';
    const key = [method, path, tags['http.status'], detail].join(' ').trim();
    if (reported.has(key)) return;
    reported.add(key);
    const error = new Error(`API ${key}`);
    error.name = 'ApiError';
    Sentry.captureException(error, {
      tags: { ...tags, 'http.method': method },
      fingerprint: ['api-error', method, path, tags['http.status'], detail],
    });
  } catch {
    // reporting must never break a request
  }
}

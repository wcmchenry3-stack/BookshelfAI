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
 *  - This is the single owner of every failure it reports, POST /scan
 *    included. Callers that also capture (ScanJobContext) skip errors for
 *    which `isReportedCentrally` is true, so there is never a second event.
 *  - A fresh sanitized Error is captured, never the AxiosError: that carries
 *    `config.headers.Authorization` and request/response bodies.
 */
import { Sentry } from './sentry';

/** 403 detail the edge/origin lockdown sends (backend/app/core/origin_auth.py). */
export const FORBIDDEN = 'forbidden';

/** Short machine codes safe to send as the `api.detail` tag. */
const KNOWN_DETAILS: ReadonlySet<string> = new Set([
  FORBIDDEN,
  // 403 the app handles (premium-only enhanced scan): recognised, not reported.
  'enhanced_requires_premium',
  // 503 scan pause/outage codes (scanErrors.ts): recognised, not reported.
  'scan_disabled',
  'enhanced_scan_disabled',
  'scan_capacity',
  'ai_budget_reached',
  'ai_unavailable',
  'scan_unavailable',
]);

/** Codes the app handles on purpose; never reported. */
const HANDLED_DETAILS: ReadonlySet<string> = new Set(
  [...KNOWN_DETAILS].filter((d) => d !== FORBIDDEN)
);

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
  return (
    path
      .split('/')
      // Safe by default: anything that could be an identifier (UUID, ISBN, hash,
      // slug, any segment with a digit, or a long one) becomes `:id`. Static
      // route words (user-books, scan, usage, wishlist, books, search, auth, me,
      // unverified) have no digits and are short, so they stay.
      .map((seg) => (UUID_RE.test(seg) || /\d/.test(seg) || seg.length > 24 ? ':id' : seg))
      .join('/')
  );
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

/**
 * True when the response interceptor reports (or already reported, or
 * deduped) this failure, so a caller must not capture it again. False for
 * errors with no response (network, non-API) and for statuses the central
 * reporter does not cover (e.g. 400/404/422), which callers keep capturing.
 */
export function isReportedCentrally(err: unknown): boolean {
  return isUnexpected(err);
}

/**
 * A capture-safe stand-in for an axios error: name, method, path template and
 * status only. The AxiosError itself carries `config.headers.Authorization`
 * and bodies. Non-axios errors are returned unchanged.
 */
export function sanitizeApiError(err: unknown): unknown {
  const e = err as (ApiErrorLike & { isAxiosError?: boolean }) | null;
  if (!e || typeof e !== 'object' || !(e.isAxiosError || e.config || e.response)) return err;
  const method = (e.config?.method ?? 'get').toUpperCase();
  const status = statusOf(err);
  const out = new Error(
    `API ${method} ${pathTemplate(e.config?.url)} ${status ?? 'no response'}`.trim()
  );
  out.name = 'ApiError';
  return out;
}

/** Report an unexpected API failure once per session. Never throws. */
export function reportApiError(err: unknown): void {
  try {
    if (!isUnexpected(err)) return;
    const tags = apiErrorTags(err);
    const path = tags['api.path'];
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

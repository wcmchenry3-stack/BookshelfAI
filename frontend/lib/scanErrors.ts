/**
 * The ONE place that knows the backend's POST /scan refusal codes and which UI
 * state each one maps to. Everything else (ScanJobContext, the scan screen)
 * works with the `ScanUiState` / `ScanNotice` kinds below, never raw codes.
 *
 * Backend contract: backend/app/api/scan.py, docs/api.md.
 *
 *   403 enhanced_requires_premium -> enhanced_refused (premium_required)
 *   429 daily_scan_limit       -> daily_limit     (Retry-After: s to UTC midnight)
 *   429 scan_in_progress       -> in_progress     (Retry-After: 2)
 *   429 enhanced_throttled     -> enhanced_refused (throttled; Retry-After: s
 *                                 until enough empty enhanced scans age out)
 *   503 scan_disabled | enhanced_scan_disabled | scan_capacity |
 *       ai_budget_reached | ai_unavailable | scan_unavailable -> paused
 *   anything else              -> generic
 *
 * There is no monthly limit (#582): the server no longer sends 402
 * scan_limit_reached or 402 enhanced_limit_reached. An older server that
 * still does gets the generic failure (any 402 is `generic`).
 *
 * The `enhanced_refused` refusals (#533) only ever answer an enhanced scan:
 * like `enhanced_scan_disabled` they stop only enhanced photos in the queue
 * drain, and an enhanced re-scan falls back to the standard scan's books.
 * The old `402 no_enhanced_credits` (and a bare 402) no longer exist.
 */

export type ScanUiState =
  | { kind: 'daily_limit'; retryAfterSeconds?: number }
  | { kind: 'in_progress'; retryAfterSeconds: number }
  | { kind: 'paused'; enhancedOnly?: boolean }
  | {
      kind: 'enhanced_refused';
      reason: EnhancedRefusalReason;
      /** Seconds until enhanced scans are allowed again (throttled only). */
      retryAfterSeconds?: number;
    }
  | { kind: 'generic' };

/** Why the server refused an enhanced scan (#533). */
export type EnhancedRefusalReason = 'premium_required' | 'throttled';

/** What the scan screen shows. `in_progress` becomes `still_processing` once retries run out. */
export type ScanNotice =
  { kind: 'daily_limit'; resetsAt: number } | { kind: 'still_processing' } | { kind: 'paused' };

/** `error` values a refused (re-queued) job carries. */
// 'monthly_limit' stays for one release (#582): a job persisted by an older build
// may still carry it, and it still counts as a refusal (#594 removes it).
export const REFUSAL_ERRORS: readonly string[] = [
  'daily_limit',
  'paused',
  'in_progress',
  'monthly_limit',
];

/** True when any queued job is waiting because the server refused it. */
export function hasRefusedJobs(jobs: readonly { status: string; error?: string }[]): boolean {
  return jobs.some((j) => j.status === 'queued' && !!j.error && REFUSAL_ERRORS.includes(j.error));
}

/** Silent retries for 429 scan_in_progress before showing "Still processing". */
export const MAX_IN_PROGRESS_RETRIES = 3;
/** Used when Retry-After is missing or unusable. */
export const DEFAULT_IN_PROGRESS_RETRY_SECONDS = 2;
/** Cap so a bad header can't stall the job for long. */
export const MAX_IN_PROGRESS_RETRY_SECONDS = 10;

/** Backend 503 `detail` codes that all mean "scanning is off right now". */
export const PAUSED_CODES: readonly string[] = [
  'scan_disabled',
  'enhanced_scan_disabled',
  'scan_capacity',
  'ai_unavailable',
  // The service's daily AI budget is used up; Free scans pause until UTC
  // midnight (#531).
  'ai_budget_reached',
  'scan_unavailable',
];

interface ErrorResponseLike {
  status?: number;
  data?: { detail?: unknown } | null;
  headers?: unknown;
}

function readHeader(headers: unknown, name: string): string | undefined {
  if (!headers || typeof headers !== 'object') return undefined;
  const h = headers as { get?: (n: string) => unknown } & Record<string, unknown>;
  if (typeof h.get === 'function') {
    const v = h.get(name);
    if (v != null) return String(v);
  }
  const key = Object.keys(h).find((k) => k.toLowerCase() === name.toLowerCase());
  return key && h[key] != null ? String(h[key]) : undefined;
}

function parseSeconds(raw: string | undefined): number | undefined {
  if (raw == null) return undefined;
  const n = Number(raw);
  return Number.isFinite(n) && n >= 0 ? Math.ceil(n) : undefined;
}

/** Map a failed POST /scan error to exactly one UI state. */
export function classifyScanError(err: unknown): ScanUiState {
  const response = (err as { response?: ErrorResponseLike } | null)?.response;
  if (!response) return { kind: 'generic' };
  const { status } = response;
  const detail = typeof response.data?.detail === 'string' ? response.data.detail : undefined;

  if (status === 403 && detail === 'enhanced_requires_premium') {
    return { kind: 'enhanced_refused', reason: 'premium_required' };
  }
  if (status === 429) {
    const retryAfterSeconds = parseSeconds(readHeader(response.headers, 'retry-after'));
    if (detail === 'daily_scan_limit') return { kind: 'daily_limit', retryAfterSeconds };
    if (detail === 'enhanced_throttled') {
      return { kind: 'enhanced_refused', reason: 'throttled', retryAfterSeconds };
    }
    if (detail === 'scan_in_progress') {
      const wait = retryAfterSeconds ?? DEFAULT_IN_PROGRESS_RETRY_SECONDS;
      return {
        kind: 'in_progress',
        retryAfterSeconds: Math.min(Math.max(wait, 1), MAX_IN_PROGRESS_RETRY_SECONDS),
      };
    }
    return { kind: 'generic' };
  }
  if (status === 503 && detail !== undefined && PAUSED_CODES.includes(detail)) {
    // Only enhanced scans are switched off; standard scans still work.
    return detail === 'enhanced_scan_disabled'
      ? { kind: 'paused', enhancedOnly: true }
      : { kind: 'paused' };
  }
  return { kind: 'generic' };
}

/** Epoch ms of the next UTC midnight after `now`. */
export function nextUtcMidnight(now: number): number {
  const d = new Date(now);
  return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() + 1);
}

/** The screen notice for a UI state, or null when the state has none (generic,
 * enhanced_refused: a banner speaks for those). */
export function toScanNotice(
  state: ScanUiState,
  now: number = Date.now(),
  exhaustedRetries = false
): ScanNotice | null {
  switch (state.kind) {
    case 'daily_limit':
      return {
        kind: 'daily_limit',
        resetsAt:
          state.retryAfterSeconds != null && state.retryAfterSeconds > 0
            ? now + state.retryAfterSeconds * 1000
            : nextUtcMidnight(now),
      };
    case 'in_progress':
      return exhaustedRetries ? { kind: 'still_processing' } : null;
    case 'paused':
      return { kind: 'paused' };
    default:
      return null;
  }
}

/** A time of day in the user's local time zone and locale. */
export function formatLocalTime(epochMs: number, locale: string): string {
  try {
    return new Date(epochMs).toLocaleTimeString(locale, { hour: 'numeric', minute: '2-digit' });
  } catch {
    return new Date(epochMs).toLocaleTimeString();
  }
}

type TFn = (key: string, options?: Record<string, unknown>) => string;

/**
 * Localised banner text for a refused enhanced scan. `t` must be bound to the
 * `scan` namespace. A throttle names the local time enhanced scans are allowed
 * again (from Retry-After) when the server sent one.
 */
export function enhancedRefusalMessage(
  state: { reason: EnhancedRefusalReason; retryAfterSeconds?: number },
  t: TFn,
  locale: string,
  now: number = Date.now()
): string {
  switch (state.reason) {
    case 'premium_required':
      return t('enhancedRequiresPremium');
    case 'throttled':
      return state.retryAfterSeconds != null && state.retryAfterSeconds > 0
        ? t('enhancedThrottled', {
            time: formatLocalTime(now + state.retryAfterSeconds * 1000, locale),
          })
        : t('enhancedThrottledLater');
  }
}

/** Localised message for a notice. `t` must be bound to the `scan` namespace. */
export function scanNoticeMessage(notice: ScanNotice, t: TFn, locale: string): string {
  switch (notice.kind) {
    case 'daily_limit':
      return t('dailyLimit', { time: formatLocalTime(notice.resetsAt, locale) });
    case 'still_processing':
      return t('stillProcessing');
    case 'paused':
      return t('scanPaused');
  }
}

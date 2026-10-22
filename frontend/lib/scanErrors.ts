/**
 * The ONE place that knows the backend's POST /scan refusal codes and which UI
 * state each one maps to. Everything else (ScanJobContext, the scan screen)
 * works with the `ScanUiState` / `ScanNotice` kinds below, never raw codes.
 *
 * Backend contract: backend/app/api/scan.py, docs/api.md.
 *
 *   402 scan_limit_reached     -> monthly_limit   (body: limit, used, period_end)
 *   402 no_enhanced_credits    -> no_enhanced_credits (pre-existing handling)
 *   429 daily_scan_limit       -> daily_limit     (Retry-After: s to UTC midnight)
 *   429 scan_in_progress       -> in_progress     (Retry-After: 2)
 *   503 scan_disabled | enhanced_scan_disabled | scan_capacity |
 *       ai_unavailable | scan_unavailable -> paused
 *   anything else              -> generic
 *
 * A bare 402 with no `detail` predates the coded responses and always meant
 * "out of enhanced credits", so it keeps that meaning.
 */

export type ScanUiState =
  | { kind: 'monthly_limit'; limit?: number; periodEnd?: string }
  | { kind: 'daily_limit'; retryAfterSeconds?: number }
  | { kind: 'in_progress'; retryAfterSeconds: number }
  | { kind: 'paused' }
  | { kind: 'no_enhanced_credits' }
  | { kind: 'generic' };

/** What the scan screen shows. `in_progress` becomes `still_processing` once retries run out. */
export type ScanNotice = (
  | { kind: 'monthly_limit'; limit?: number; periodEnd?: string }
  | { kind: 'daily_limit'; resetsAt: number }
  | { kind: 'still_processing' }
  | { kind: 'paused' }
) & {
  /** The refused job, when it can be retried from the notice ("Try again"). */
  jobId?: string;
};

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
  'scan_unavailable',
];

interface ErrorResponseLike {
  status?: number;
  data?: { detail?: unknown; limit?: unknown; period_end?: unknown } | null;
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

  if (status === 402) {
    if (detail === 'scan_limit_reached') {
      const { limit, period_end: periodEnd } = response.data ?? {};
      return {
        kind: 'monthly_limit',
        limit: typeof limit === 'number' ? limit : undefined,
        periodEnd: typeof periodEnd === 'string' ? periodEnd : undefined,
      };
    }
    if (detail === undefined || detail === 'no_enhanced_credits') {
      return { kind: 'no_enhanced_credits' };
    }
    return { kind: 'generic' };
  }
  if (status === 429) {
    const retryAfterSeconds = parseSeconds(readHeader(response.headers, 'retry-after'));
    if (detail === 'daily_scan_limit') return { kind: 'daily_limit', retryAfterSeconds };
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
    return { kind: 'paused' };
  }
  return { kind: 'generic' };
}

/** Epoch ms of the next UTC midnight after `now`. */
export function nextUtcMidnight(now: number): number {
  const d = new Date(now);
  return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() + 1);
}

/** The screen notice for a UI state, or null when the state has none (generic, credits). */
export function toScanNotice(
  state: ScanUiState,
  now: number = Date.now(),
  exhaustedRetries = false
): ScanNotice | null {
  switch (state.kind) {
    case 'monthly_limit':
      return { kind: 'monthly_limit', limit: state.limit, periodEnd: state.periodEnd };
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

/** `YYYY-MM-DD` (a UTC calendar date) formatted for the user's locale; null if unparseable. */
export function formatPeriodEnd(periodEnd: string | undefined, locale: string): string | null {
  if (!periodEnd || !/^\d{4}-\d{2}-\d{2}$/.test(periodEnd)) return null;
  const d = new Date(`${periodEnd}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return null;
  try {
    return d.toLocaleDateString(locale, { timeZone: 'UTC', dateStyle: 'long' });
  } catch {
    return periodEnd;
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

/** Localised message for a notice. `t` must be bound to the `scan` namespace. */
export function scanNoticeMessage(notice: ScanNotice, t: TFn, locale: string): string {
  switch (notice.kind) {
    case 'monthly_limit': {
      const head =
        notice.limit != null ? t('monthlyLimit', { count: notice.limit }) : t('monthlyLimitAny');
      const date = formatPeriodEnd(notice.periodEnd, locale);
      return date ? `${head} ${t('monthlyResets', { date })}` : head;
    }
    case 'daily_limit':
      return t('dailyLimit', { time: formatLocalTime(notice.resetsAt, locale) });
    case 'still_processing':
      return t('stillProcessing');
    case 'paused':
      return t('scanPaused');
  }
}

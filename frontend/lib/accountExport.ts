/**
 * "Export my data" (#694): asks the server for a copy of the account's data.
 *
 * `POST /account/export` only queues the export (`202`); the server builds a
 * JSON file and emails the account's address a link that works for 7 days.
 * The body carries the app's language so the email is written in it. The
 * request is keyed (`lib/idempotency.ts`), so a retry after a lost response
 * replays the first answer instead of asking twice.
 *
 * Outcomes the Settings row shows:
 *  - `queued`: the email is on its way (also when the server says the keyed
 *    request was already applied, `409 idempotency_outcome_unknown`);
 *  - `limited`: one export a day (`409 export_limit`); `hours` until the next;
 *  - `unavailable`: the server is not set up for exports yet
 *    (`503 export_not_configured`);
 *  - `failed`: anything else (offline, a server error).
 */
import { api } from './api';
import { toApiError } from './apiError';
import { isIdempotencyOutcomeUnknown } from './idempotency';

const PATH = '/account/export';

/** The languages the server writes the email in (backend `ExportLocale`). */
const SERVER_LOCALES: readonly string[] = [
  'ar',
  'de',
  'en',
  'es',
  'fr-CA',
  'he',
  'hi',
  'ja',
  'ko',
  'nl',
  'pt',
  'ru',
  'zh',
];

const HOUR_MS = 3_600_000;
/** Used when a cap refusal carries no usable `Retry-After`: the whole window. */
const CAP_WINDOW_HOURS = 24;

export type AccountExportResult =
  | { kind: 'queued' }
  | { kind: 'limited'; hours: number }
  | { kind: 'unavailable' }
  | { kind: 'failed' };

/** The email language for the app's `language` (English when the server has none). */
export function exportLocale(language: string | undefined): string {
  if (language && SERVER_LOCALES.includes(language)) return language;
  const base = language?.split('-')[0];
  return base && SERVER_LOCALES.includes(base) ? base : 'en';
}

function hoursUntil(retryAfterMs: number | undefined): number {
  if (retryAfterMs === undefined || !Number.isFinite(retryAfterMs)) return CAP_WINDOW_HOURS;
  return Math.max(1, Math.ceil(retryAfterMs / HOUR_MS));
}

/** Asks for an export. Never rejects. */
export async function requestAccountExport(
  language: string | undefined
): Promise<AccountExportResult> {
  try {
    await api.post(PATH, { locale: exportLocale(language) });
    return { kind: 'queued' };
  } catch (err) {
    if (isIdempotencyOutcomeUnknown(err)) return { kind: 'queued' };
    const { status, code, retryAfterMs } = toApiError(err);
    if (status === 409 && code === 'export_limit') {
      return { kind: 'limited', hours: hoursUntil(retryAfterMs) };
    }
    if (status === 503 && code === 'export_not_configured') return { kind: 'unavailable' };
    return { kind: 'failed' };
  }
}

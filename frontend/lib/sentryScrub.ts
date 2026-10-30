/**
 * Sentry `beforeSend` scrubber (#736).
 *
 * #727 makes every explicit `captureException` send a sanitized `ApiError`,
 * but a raw AxiosError nobody catches still reaches Sentry's global
 * unhandled-rejection handler. It carries `config.headers.Authorization`
 * (the bearer token) and request/response bodies. Today the SDK does not
 * attach them (sendDefaultPii=false, no ExtraErrorData); this makes that
 * independent of SDK defaults and integrations.
 *
 *  1. If `hint.originalException` is an AxiosError, the exception entry is
 *     replaced by the same sanitized `ApiError` shape as apiErrorReporting.
 *  2. Defence in depth: `extra`, `contexts` and each exception's
 *     `mechanism.data` are walked and sensitive keys redacted, and any
 *     `Bearer <token>` string masked.
 *
 * Runs once per event (not on the frame path); bounded depth, never throws.
 */
import axios from 'axios';
import { sanitizeApiError } from './apiErrorReporting';

const FILTERED = '[Filtered]';
const MAX_DEPTH = 8;
const REDACT_KEYS: ReadonlySet<string> = new Set([
  'authorization',
  'config',
  'headers',
  'request',
  'cookie',
  'set-cookie',
]);
const BEARER_RE = /Bearer\s+[A-Za-z0-9._~+/=-]+/gi;

/** Mask `Bearer <token>` occurrences in a string. */
export function redactBearer(s: string): string {
  return s.replace(BEARER_RE, `Bearer ${FILTERED}`);
}

function scrubValue(
  v: unknown,
  depth: number,
  seen: WeakSet<object>,
  inResponse: boolean
): unknown {
  if (typeof v === 'string') return redactBearer(v);
  if (v === null || typeof v !== 'object') return v;
  if (depth >= MAX_DEPTH) return FILTERED;
  if (seen.has(v)) return '[Circular]';
  seen.add(v);
  if (Array.isArray(v)) return v.map((x) => scrubValue(x, depth + 1, seen, inResponse));
  const out: Record<string, unknown> = {};
  for (const [k, val] of Object.entries(v as Record<string, unknown>)) {
    const key = k.toLowerCase();
    if (REDACT_KEYS.has(key) || (inResponse && key === 'data')) {
      out[k] = FILTERED;
    } else {
      out[k] = scrubValue(val, depth + 1, seen, inResponse || key === 'response');
    }
  }
  return out;
}

type ExceptionValue = { type?: string; value?: string; mechanism?: { data?: unknown } };
type EventLike = {
  extra?: unknown;
  contexts?: unknown;
  exception?: { values?: ExceptionValue[] };
};

/** Scrub a Sentry event in place and return it. Never throws; on error returns the event. */
export function scrubSentryEvent<T>(event: T, hint?: { originalException?: unknown }): T {
  try {
    const e = event as unknown as EventLike;
    const values = e.exception?.values;
    if (Array.isArray(values)) {
      if (axios.isAxiosError(hint?.originalException)) {
        const safe = sanitizeApiError(hint.originalException) as Error;
        for (const ex of values) {
          ex.type = safe.name;
          ex.value = safe.message;
        }
      }
      for (const ex of values) {
        if (typeof ex.value === 'string') ex.value = redactBearer(ex.value);
        if (ex.mechanism?.data !== undefined) {
          ex.mechanism.data = scrubValue(ex.mechanism.data, 0, new WeakSet(), false);
        }
      }
    }
    if (e.extra !== undefined) e.extra = scrubValue(e.extra, 0, new WeakSet(), false);
    if (e.contexts !== undefined) e.contexts = scrubValue(e.contexts, 0, new WeakSet(), false);
  } catch {
    // never throw from beforeSend
  }
  return event;
}

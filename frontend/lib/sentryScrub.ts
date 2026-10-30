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
 *  1. If `hint.originalException` is an AxiosError, the exception entry for
 *     that error (the last `values` entry) becomes the sanitized `ApiError`.
 *  2. `extra`, `contexts` and each exception's `mechanism.data` are rebuilt
 *     (never mutated, so frozen events work) with sensitive keys redacted
 *     and token-looking strings masked.
 *  3. Fail closed: each of those fields is scrubbed in its own try block; if
 *     scrubbing throws, the field is dropped, never sent half-scrubbed.
 *
 * Import rule: this module is imported by lib/sentry.ts, which must
 * self-init before any other module evaluates (rule #13). It therefore
 * imports only the leaf `apiErrorSanitize` and detects AxiosError
 * structurally (no axios import). Runs once per event, never on the frame path.
 */
import { sanitizeApiError } from './apiErrorSanitize';

const FILTERED = '[Filtered]';
const MAX_DEPTH = 8;

// Always redacted, at any depth, case-insensitive.
const ALWAYS_KEYS: ReadonlySet<string> = new Set([
  'authorization',
  'cookie',
  'set-cookie',
  'proxy-authorization',
]);
const HTTP_CONTAINERS: ReadonlySet<string> = new Set(['config', 'request', 'response']);
const XHR_REQUEST_KEYS = ['headers', '_headers', 'responseURL', 'readyState', 'body', 'data'];

// Linear-time: the lookahead + backreference emulates a possessive quantifier,
// so a failed match never backtracks into the token body.
const BEARER_RE = /Bearer\s+(?=([A-Za-z0-9._~+/=-]+))\1/gi;
const JWT_RE = /eyJ(?=([A-Za-z0-9_-]+))\1\.(?=([A-Za-z0-9_-]+))\2\.(?=([A-Za-z0-9_-]*))\3/g;
const QUERY_SECRET_RE =
  /\b(access_token|refresh_token|id_token|token|api[_-]?key|password|secret)=(?=([^&\s"']+))\2/gi;

/** Mask bearer tokens, raw JWTs and token-looking query params in a string. */
export function redactString(s: string): string {
  return s
    .replace(BEARER_RE, `Bearer ${FILTERED}`)
    .replace(JWT_RE, '[jwt]')
    .replace(QUERY_SECRET_RE, `$1=${FILTERED}`);
}

type Obj = Record<string, unknown>;

// Set whenever scrubbing alters anything, so an untouched event is returned
// as the same object. Synchronous module state: beforeSend is not re-entrant.
let changed = false;
function mark<V>(v: V): V {
  changed = true;
  return v;
}
function mask(redacted: string, original: string): string {
  if (redacted !== original) changed = true;
  return redacted;
}

function isPlainObject(v: unknown): v is Obj {
  if (v === null || typeof v !== 'object') return false;
  const proto = Object.getPrototypeOf(v);
  return proto === Object.prototype || proto === null;
}

function hasAny(o: Obj, keys: readonly string[]): boolean {
  return keys.some((k) => k in o);
}

/** Should `key: val` inside `obj` (reached via `selfKey`) be replaced wholesale? */
function shouldRedact(key: string, val: unknown, obj: Obj, selfKey: string): boolean {
  if (ALWAYS_KEYS.has(key)) return true;
  if (key === 'config') return isPlainObject(val) && hasAny(val, ['headers', 'url']);
  if (key === 'request') return isPlainObject(val) && hasAny(val, XHR_REQUEST_KEYS);
  if (key === 'headers' || key === 'data') {
    return HTTP_CONTAINERS.has(selfKey) && hasAny(obj, ['url', 'method', 'status']);
  }
  return false;
}

function scrubValue(
  v: unknown,
  depth: number,
  ancestors: WeakSet<object>,
  selfKey: string
): unknown {
  if (typeof v === 'string') return mask(redactString(v), v);
  if (v === null || typeof v !== 'object') return v;
  if (v instanceof Date) return v;
  if (v instanceof Error) return mark({ name: v.name, message: redactString(String(v.message)) });
  if (!Array.isArray(v) && !isPlainObject(v)) return mark('[Object]');
  if (depth >= MAX_DEPTH) return mark(FILTERED);
  if (ancestors.has(v)) return mark('[Circular]');
  ancestors.add(v);
  try {
    if (Array.isArray(v)) return v.map((x) => scrubValue(x, depth + 1, ancestors, selfKey));
    const out: Obj = {};
    for (const [k, val] of Object.entries(v)) {
      const key = k.toLowerCase();
      out[k] = shouldRedact(key, val, v, selfKey)
        ? mark(FILTERED)
        : scrubValue(val, depth + 1, ancestors, key);
    }
    return out;
  } finally {
    ancestors.delete(v);
  }
}

type ExceptionValue = { type?: string; value?: string; mechanism?: { data?: unknown } };
type EventLike = {
  extra?: unknown;
  contexts?: unknown;
  exception?: { values?: ExceptionValue[] };
};

function scrubException(ex: ExceptionValue, replaceWith?: Error): ExceptionValue {
  const out: ExceptionValue = { ...ex };
  if (replaceWith) {
    changed = true;
    out.type = replaceWith.name;
    out.value = replaceWith.message;
  } else if (typeof out.value === 'string') {
    out.value = mask(redactString(out.value), out.value);
  }
  if (ex.mechanism) {
    const mechanism = { ...ex.mechanism };
    if (mechanism.data !== undefined) {
      try {
        mechanism.data = scrubValue(mechanism.data, 0, new WeakSet(), 'data');
      } catch {
        delete mechanism.data; // fail closed
        changed = true;
      }
    }
    out.mechanism = mechanism;
  }
  return out;
}

/**
 * Return a scrubbed copy of a Sentry event (the input is never mutated, so
 * frozen events work). A field that cannot be scrubbed is removed.
 */
export function scrubSentryEvent<T>(event: T, hint?: { originalException?: unknown }): T {
  if (event === null || typeof event !== 'object') return event;
  changed = false;
  const out = { ...(event as unknown as EventLike) } as EventLike;

  for (const field of ['extra', 'contexts'] as const) {
    if (out[field] === undefined) continue;
    try {
      out[field] = scrubValue(out[field], 0, new WeakSet(), field);
    } catch {
      delete out[field]; // fail closed
      changed = true;
    }
  }

  const values = out.exception?.values;
  if (Array.isArray(values)) {
    try {
      const orig = hint?.originalException as { isAxiosError?: unknown } | null | undefined;
      const axiosIdx = orig && orig.isAxiosError === true ? values.length - 1 : -1;
      const safe = axiosIdx >= 0 ? (sanitizeApiError(orig) as Error) : undefined;
      out.exception = {
        ...out.exception,
        values: values.map((ex, i) => scrubException(ex, i === axiosIdx ? safe : undefined)),
      };
    } catch {
      delete out.exception; // fail closed
      changed = true;
    }
  }
  // Nothing needed scrubbing: hand back the original object (identity).
  return changed ? (out as unknown as T) : event;
}

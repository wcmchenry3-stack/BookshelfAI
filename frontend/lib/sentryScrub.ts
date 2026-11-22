/**
 * Sentry `beforeSend` / `beforeBreadcrumb` scrubber (#736).
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
 *  2. `extra`, `contexts`, `logentry`, `message`, `request` and each
 *     exception's `mechanism.data` are rebuilt (never mutated, so frozen
 *     events work) with sensitive keys redacted and token-looking strings
 *     masked. Breadcrumbs get the same treatment via `scrubBreadcrumb`.
 *  3. Fail closed: each field is scrubbed in its own try block; if scrubbing
 *     throws, the field is dropped, never sent half-scrubbed. If a copy of the
 *     event itself cannot be built, `scrubSentryEvent` returns `null` (the
 *     event is dropped) rather than sending it unscrubbed.
 *
 * Import rule: this module is imported by lib/sentry.ts, which must
 * self-init before any other module evaluates (rule #13). It therefore
 * imports only the leaf `apiErrorSanitize` and detects AxiosError
 * structurally (no axios import). Runs once per event, never on the frame path.
 * All string matching is linear-time (see `redactString`).
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
  'token',
  'jwt',
  'access_token',
  'accesstoken',
  'refresh_token',
  'refreshtoken',
  'id_token',
  'idtoken',
  'password',
  'passwd',
  'secret',
  'api_key',
  'apikey',
  'api-key',
]);
// Transactions only (#715): URL query / fragment attributes are dropped wholesale.
const QUERY_KEYS: ReadonlySet<string> = new Set([
  'http.query',
  'url.query',
  'http.fragment',
  'url.fragment',
  'query_string',
]);
const HTTP_CONTAINERS: ReadonlySet<string> = new Set(['config', 'request']);
const XHR_REQUEST_KEYS = [
  'headers',
  '_headers',
  '_header',
  'responseURL',
  'readyState',
  'body',
  'data',
];
const CONFIG_KEYS = ['headers', 'url', 'data', 'baseURL', 'method'];

// Linear-time: the lookahead + backreference emulates a possessive quantifier,
// so a failed match never backtracks into the token body. Matches are
// non-overlapping and each consumes what it matched.
const BEARER_RE = /Bearer\s+(?=([A-Za-z0-9._~+/=-]+))\1/gi;
const QUERY_SECRET_RE =
  /\b(access_token|refresh_token|id_token|token|api[_-]?key|password|secret)=(?=([^&\s"']+))\2/gi;
const AUTH_HEADER_RE =
  /\b(authorization|proxy-authorization)\s*:\s*(?=([^\s,;"']+(?:[ \t]+[^\s,;"']+)?))\2/gi;
const COOKIE_HEADER_RE = /\b(set-cookie|cookie)\s*:\s*(?=([^\r\n]+))\2/gi;

function isTokenChar(code: number): boolean {
  return (
    (code >= 48 && code <= 57) || // 0-9
    (code >= 65 && code <= 90) || // A-Z
    (code >= 97 && code <= 122) || // a-z
    code === 95 || // _
    code === 45 // -
  );
}

/** Index just past the run of token chars starting at `from`. */
function skipToken(s: string, from: number): number {
  let j = from;
  while (j < s.length && isTokenChar(s.charCodeAt(j))) j++;
  return j;
}

/**
 * Replace raw JWTs (`eyJ<seg>.<seg>.<seg>`) with `[jwt]`. A manual scan, not a
 * regex: every failed candidate jumps past its whole token run, so each
 * character is examined a bounded number of times (linear).
 */
export function redactJwts(s: string): string {
  let i = s.indexOf('eyJ');
  if (i === -1) return s;
  let out = '';
  let last = 0;
  while (i !== -1) {
    let next: number;
    if (i > 0 && isTokenChar(s.charCodeAt(i - 1))) {
      next = skipToken(s, i); // mid-token: not the start of a JWT
    } else {
      const e1 = skipToken(s, i + 3);
      let end = -1;
      if (e1 > i + 3 && s.charAt(e1) === '.') {
        const e2 = skipToken(s, e1 + 1);
        if (e2 > e1 + 1 && s.charAt(e2) === '.') end = skipToken(s, e2 + 1);
      }
      if (end === -1) {
        next = e1; // failed: jump past the first run
      } else {
        out += s.slice(last, i) + '[jwt]';
        last = end;
        next = end;
      }
    }
    i = s.indexOf('eyJ', next);
  }
  return out + s.slice(last);
}

/** Mask auth/cookie header lines, bearer tokens, raw JWTs and token query params. */
export function redactString(s: string): string {
  return redactJwts(
    s
      .replace(AUTH_HEADER_RE, `$1: ${FILTERED}`)
      .replace(COOKIE_HEADER_RE, `$1: ${FILTERED}`)
      .replace(BEARER_RE, `Bearer ${FILTERED}`)
  ).replace(QUERY_SECRET_RE, `$1=${FILTERED}`);
}

type Obj = Record<string, unknown>;
type St = { changed: boolean; str?: (s: string) => string; txn?: boolean };

function mark<V>(st: St, v: V): V {
  st.changed = true;
  return v;
}
function mask(st: St, redacted: string, original: string): string {
  if (redacted !== original) st.changed = true;
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
function shouldRedact(
  key: string,
  val: unknown,
  obj: Obj,
  selfKey: string,
  txn?: boolean
): boolean {
  if (ALWAYS_KEYS.has(key)) return true;
  if (txn && QUERY_KEYS.has(key)) return true;
  if (key === 'config') return isPlainObject(val) && hasAny(val, CONFIG_KEYS);
  if (key === 'request') return isPlainObject(val) && hasAny(val, XHR_REQUEST_KEYS);
  if (key === 'headers' || key === 'data') {
    if (selfKey === 'response') return true;
    return HTTP_CONTAINERS.has(selfKey) && hasAny(obj, ['url', 'method', 'status']);
  }
  return false;
}

function scrubValue(
  v: unknown,
  depth: number,
  ancestors: WeakSet<object>,
  selfKey: string,
  st: St
): unknown {
  if (typeof v === 'string') {
    const r = redactString(v);
    return mask(st, st.str ? st.str(r) : r, v);
  }
  if (v === null || typeof v !== 'object') return v;
  if (v instanceof Date) return v;
  if (v instanceof Error) {
    return mark(st, { name: v.name, message: redactString(String(v.message)) });
  }
  if (!Array.isArray(v) && !isPlainObject(v)) return mark(st, '[Object]');
  if (depth >= MAX_DEPTH) return mark(st, FILTERED);
  if (ancestors.has(v)) return mark(st, '[Circular]');
  ancestors.add(v);
  try {
    if (Array.isArray(v)) return v.map((x) => scrubValue(x, depth + 1, ancestors, selfKey, st));
    const out: Obj = {};
    for (const [k, val] of Object.entries(v)) {
      const key = k.toLowerCase();
      out[k] = shouldRedact(key, val, v, selfKey, st.txn)
        ? mark(st, FILTERED)
        : scrubValue(val, depth + 1, ancestors, key, st);
    }
    return out;
  } finally {
    ancestors.delete(v);
  }
}

type ExceptionValue = { type?: string; value?: string; mechanism?: { data?: unknown } };

function scrubException(ex: ExceptionValue, st: St, replaceWith?: Error): ExceptionValue {
  const out: ExceptionValue = { ...ex };
  if (replaceWith) {
    st.changed = true;
    out.type = replaceWith.name;
    out.value = replaceWith.message;
  } else if (typeof out.value === 'string') {
    out.value = mask(st, redactString(out.value), out.value);
  }
  if (ex.mechanism) {
    const mechanism = { ...ex.mechanism };
    if (mechanism.data !== undefined) {
      try {
        mechanism.data = scrubValue(mechanism.data, 0, new WeakSet(), 'data', st);
      } catch {
        delete mechanism.data; // fail closed
        st.changed = true;
      }
    }
    out.mechanism = mechanism;
  }
  return out;
}

/** Top-level `event.request`: header keys, url / query_string, cookies. */
function scrubRequest(req: unknown, st: St): unknown {
  if (!isPlainObject(req)) return mark(st, FILTERED);
  const out: Obj = { ...req };
  if (out.headers !== undefined) {
    out.headers = scrubValue(out.headers, 0, new WeakSet(), 'headers', st);
  }
  for (const k of ['url', 'query_string'] as const) {
    if (out[k] !== undefined) out[k] = scrubValue(out[k], 0, new WeakSet(), k, st);
  }
  if (st.txn && out.query_string !== undefined) out.query_string = mark(st, FILTERED);
  if (out.cookies !== undefined) out.cookies = mark(st, FILTERED);
  return out;
}

/** Scrub `out[key]` with `fn`; on failure delete the field (fail closed). */
function guarded(out: Obj, key: string, st: St, fn: (v: unknown) => unknown): void {
  if (out[key] === undefined) return;
  try {
    out[key] = fn(out[key]);
  } catch {
    delete out[key];
    st.changed = true;
  }
}

/**
 * Return a scrubbed copy of a Sentry event (the input is never mutated, so
 * frozen events work), or the same object when nothing needed scrubbing. A
 * field that cannot be scrubbed is removed; if the event cannot even be
 * copied, returns `null` so it is dropped rather than sent unscrubbed.
 */
export function scrubSentryEvent<T>(event: T, hint?: { originalException?: unknown }): T | null {
  if (event === null || typeof event !== 'object') return event;
  const st: St = { changed: false };
  let out: Obj;
  try {
    out = { ...(event as unknown as Obj) };
  } catch {
    return null;
  }

  for (const field of ['extra', 'contexts', 'message', 'logentry'] as const) {
    guarded(out, field, st, (v) => scrubValue(v, 0, new WeakSet(), field, st));
  }
  guarded(out, 'request', st, (v) => scrubRequest(v, st));

  try {
    const exception = out.exception as { values?: ExceptionValue[] } | undefined;
    const values = exception?.values;
    if (Array.isArray(values)) {
      const orig = hint?.originalException as { isAxiosError?: unknown } | null | undefined;
      const axiosIdx = orig && orig.isAxiosError === true ? values.length - 1 : -1;
      const safe = axiosIdx >= 0 ? (sanitizeApiError(orig) as Error) : undefined;
      out.exception = {
        ...exception,
        values: values.map((ex, i) => scrubException(ex, st, i === axiosIdx ? safe : undefined)),
      };
    }
  } catch {
    delete out.exception; // fail closed
    st.changed = true;
  }
  return st.changed ? (out as unknown as T) : event;
}

/**
 * `beforeBreadcrumb`: the same scrub for `message` and `data` (e.g. an http
 * breadcrumb's url with a token query param). Cheap: breadcrumbs are small.
 * Same identity / fail-closed rules; a breadcrumb that cannot be copied is dropped.
 */
export function scrubBreadcrumb<T>(breadcrumb: T): T | null {
  if (breadcrumb === null || typeof breadcrumb !== 'object') return breadcrumb;
  const st: St = { changed: false };
  let out: Obj;
  try {
    out = { ...(breadcrumb as unknown as Obj) };
  } catch {
    return null;
  }
  for (const field of ['message', 'data'] as const) {
    guarded(out, field, st, (v) => scrubValue(v, 0, new WeakSet(), field, st));
  }
  return st.changed ? (out as unknown as T) : breadcrumb;
}

// ---- Transactions (#715) ---------------------------------------------------

const EMAIL_LOCAL = /[A-Za-z0-9._%+-]/;
const EMAIL_DOMAIN = /[A-Za-z0-9.-]/;
const isWs = (c: number) => c === 32 || (c >= 9 && c <= 13);
const isWord = (c: number) =>
  (c >= 48 && c <= 57) || (c >= 65 && c <= 90) || (c >= 97 && c <= 122) || c === 95;

/** Replace `local@domain.tld` with `[email]` (ASCII local part only). Linear. */
export function redactEmails(s: string): string {
  let at = s.indexOf('@');
  if (at === -1) return s;
  let out = '';
  let last = 0;
  while (at !== -1) {
    let a = at;
    while (a > last && EMAIL_LOCAL.test(s.charAt(a - 1))) a--;
    let b = at + 1;
    while (b < s.length && EMAIL_DOMAIN.test(s.charAt(b))) b++;
    let e = b;
    while (e > at + 1 && s.charCodeAt(e - 1) === 46) e--; // trailing dots
    const domain = s.slice(at + 1, e);
    if (a < at && domain.includes('.') && !domain.startsWith('.')) {
      out += s.slice(last, a) + '[email]';
      last = e;
      b = e;
    }
    at = s.indexOf('@', Math.max(b, at + 1));
  }
  return out + s.slice(last);
}

/**
 * Drop the query string / fragment of every `http(s)://` URL in `s`, and of `s`
 * itself when it is an absolute path. One left-to-right pass: each character is
 * visited a bounded number of times (no backtracking).
 */
function stripUrlQueries(s: string): string {
  let out = '';
  let last = 0;
  const cut = (from: number): number => {
    // from = index just after the scheme / the path start; returns end of the URL token.
    let j = from;
    while (j < s.length) {
      const c = s.charCodeAt(j);
      if (isWs(c)) return j;
      if (c === 63 || c === 35) {
        out += s.slice(last, j);
        while (j < s.length && !isWs(s.charCodeAt(j))) j++;
        last = j;
        return j;
      }
      j++;
    }
    return j;
  };
  // URL schemes are case-insensitive (`HTTPS://`). Scan an ASCII-only
  // lower-cased copy: A-Z -> a-z keeps every index identical to `s` (unlike
  // toLowerCase on non-ASCII text), and cutting still happens on `s`.
  const lower = s.replace(/[A-Z]/g, (c) => c.toLowerCase());
  let i = 0;
  if (s.charCodeAt(0) === 47) i = cut(1);
  for (let h = lower.indexOf('http', i); h !== -1; h = lower.indexOf('http', i)) {
    let p = h + 4;
    if (lower.charAt(p) === 's') p++;
    if (s.startsWith('://', p) && (h === 0 || !isWord(s.charCodeAt(h - 1)))) {
      i = cut(p + 3);
    } else {
      i = h + 4;
    }
  }
  return last === 0 ? s : out + s.slice(last);
}

function scrubTransactionString(s: string): string {
  return redactEmails(stripUrlQueries(s));
}

/**
 * `beforeSendTransaction`: the event scrub rules (tokens, headers, bearer/JWT)
 * applied to the transaction name, tags, extra, contexts, request, breadcrumbs
 * and every span (description, data, tags), plus URL query/fragment removal
 * and email masking. Same identity / fail-closed rules as `scrubSentryEvent`:
 * a field that cannot be scrubbed is deleted; an uncopyable transaction is dropped.
 */
export function scrubSentryTransaction<T>(event: T): T | null {
  if (event === null || typeof event !== 'object') return event;
  const st: St = { changed: false, str: scrubTransactionString, txn: true };
  let out: Obj;
  try {
    out = { ...(event as unknown as Obj) };
  } catch {
    return null;
  }
  for (const field of [
    'transaction',
    'tags',
    'extra',
    'contexts',
    'breadcrumbs',
    'spans',
  ] as const) {
    guarded(out, field, st, (v) => scrubValue(v, 0, new WeakSet(), field, st));
  }
  guarded(out, 'request', st, (v) => scrubRequest(v, st));
  return st.changed ? (out as unknown as T) : event;
}

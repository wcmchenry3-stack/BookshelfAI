/**
 * SessionLogger — lightweight circular buffer that captures recent console
 * warnings and errors so they can be attached to feedback submissions.
 *
 * Call `SessionLogger.init()` once at app startup (before any other imports
 * that might log). The buffer wraps at MAX_ENTRIES so memory use is bounded.
 *
 * Pipeline per line: budgeted serialise (aborts once ~MAX_WORK_CHARS are
 * produced) -> redact the joined text (at most MAX_WORK_CHARS) -> drop any
 * partial token left at a clip -> cut to MAX_LINE_CHARS. getLogs() is capped at
 * MAX_LOG_BYTES.
 */

const MAX_ENTRIES = 200;
/** Each stored line (message part) is cut at this many characters. */
const MAX_LINE_CHARS = 1000;
/**
 * Redaction runs on at most this many characters (before the final cut to
 * MAX_LINE_CHARS), so a secret that straddles the 1,000-char cut is already
 * fully redacted, and the scanners never see a huge string.
 */
const MAX_WORK_CHARS = 4000;
const TRUNCATION_MARKER = '…[truncated]';
/** getLogs() never returns more than this many UTF-8 bytes (100 KB). */
const MAX_LOG_BYTES = 100 * 1024;
const UNSERIALIZABLE = '[unserializable]';
const MAX_ARRAY_ITEMS = 100;
/** Private-use character inserted where an argument or the joined text was clipped. */
const CLIP = '\uE000';

interface LogEntry {
  level: 'warn' | 'error';
  ts: string; // ISO timestamp
  msg: string;
}

const buffer: LogEntry[] = [];
let initialised = false;
// Save originals so _reset() can fully undo the patch (used in tests)
let originalWarn: typeof console.warn | null = null;
let originalError: typeof console.error | null = null;

/** base64url characters: [A-Za-z0-9_-] */
const isTok = (c: number): boolean =>
  (c >= 48 && c <= 57) || (c >= 65 && c <= 90) || (c >= 97 && c <= 122) || c === 45 || c === 95;
/** RFC 5322 atext characters (allowed in an email local part), plus '.'. */
const isLocal = (c: number): boolean =>
  isTok(c) ||
  c === 46 || // .
  c === 37 || // %
  c === 43 || // +
  c === 39 || // '
  c === 47 || // /
  c === 61 || // =
  c === 33 || // !
  c === 35 || // #
  c === 36 || // $
  c === 38 || // &
  c === 42 || // *
  c === 63 || // ?
  c === 94 || // ^
  c === 123 || // {
  c === 124 || // |
  c === 125 || // }
  c === 126; // ~
const isDomain = (c: number): boolean => isTok(c) || c === 46;
/** Characters a token or email that a clip may have split can consist of. */
const isTailChar = (c: number): boolean => isLocal(c) || c === 64;

// The keyed rules (AUTH, BEARER, KEY) start on a fixed keyword and are followed
// by single character classes or by disjoint alternatives, so each start
// position costs O(1) plus the length of the match it consumes: linear overall.
// JWT and email are manual scans, because "class+ then literal" regexes are
// quadratic on inputs like 'a'.repeat(n). The key is NOT anchored with a
// leading `[A-Za-z0-9_-]*` prefix (that would be quadratic); matching the
// keyword anywhere also catches `client_secret=`, `authToken=` and so on.
const AUTH_RE =
  /(Authorization["']?\s*[:=]\s*)("(?:[^"\\]|\\.)*"?|'(?:[^'\\]|\\.)*'?|Digest\s+[^\r\n}\uE000]*|(?:(?:Basic|Bearer|Token|Negotiate|NTLM|ApiKey)\s+)?[^\s"',;}\uE000]+)/gi;
const BEARER_RE = /\b(Bearer)(?:\s*:\s*|\s+)["']?[A-Za-z0-9\-._~+/]{8,}=*["']?/gi;
const KEY_RE =
  /(token|secret(?:[_-]?key)?|password|passwd|api[_-]?key)(["']?\s*[:=]\s*)("(?:[^"\\]|\\.)*"?|'(?:[^'\\]|\\.)*'?|[^,}&;\s"'\uE000]+)/gi;
const JWT_FRAGMENT_RE = /eyJ[A-Za-z0-9_-]{10,}/g;

/** Replace a matched value with [redacted], keeping surrounding quotes. */
function maskValue(value: string): string {
  const q = value.charAt(0);
  if (q === '"' || q === "'") {
    return q + '[redacted]' + (value.length > 1 && value.endsWith(q) ? q : '');
  }
  return '[redacted]';
}

/**
 * Replace JWT-shaped tokens (eyJ.seg.seg). Linear: one pass; a candidate must
 * not directly follow an alphanumeric character; after a failed candidate the
 * scan resumes after its first segment, because any 'eyJ' inside that run
 * would end its first segment at the same place and fail the same way. Two
 * glued JWTs are split where the signature run contains 'eyJ' and is followed
 * by '.'.
 */
function redactJwt(text: string): string {
  let out = '';
  let last = 0;
  let from = 0;
  let forced = -1; // a candidate start that skips the boundary check (glued JWT)
  for (;;) {
    const i = text.indexOf('eyJ', from);
    if (i === -1) break;
    from = i + 3;
    if (i !== forced && i > 0) {
      const prev = text.charCodeAt(i - 1);
      if (isTok(prev) && prev !== 45 && prev !== 95) continue; // inside an alphanumeric run
    }
    let p = i + 3;
    while (p < text.length && isTok(text.charCodeAt(p))) p++;
    if (text.charCodeAt(p) !== 46) {
      from = p;
      continue;
    }
    let q = p + 1;
    while (q < text.length && isTok(text.charCodeAt(q))) q++;
    if (q === p + 1 || text.charCodeAt(q) !== 46) {
      from = q;
      continue;
    }
    let r = q + 1;
    while (r < text.length && isTok(text.charCodeAt(r))) r++;
    if (text.charCodeAt(r) === 46) {
      // A signature followed by '.' is abnormal: probably glued JWTs.
      const g = text.indexOf('eyJ', q + 1);
      if (g !== -1 && g < r) {
        r = g;
        forced = g;
      }
    }
    out += text.slice(last, i) + '[jwt]';
    last = r;
    from = r;
  }
  const res = last === 0 ? text : out + text.slice(last);
  return res.indexOf('eyJ') === -1 ? res : res.replace(JWT_FRAGMENT_RE, '[jwt]');
}

const DOMAIN_RE = /^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$/; // classes are disjoint: linear

/**
 * Replace email addresses by expanding left/right from each '@'. Linear
 * overall: the left scan stops at the previous '@' or match, the right scan
 * stops at the next '@'. A `key=` prefix on the left is kept (`a=x@y.com` ->
 * `a=[email]`).
 */
function redactEmail(text: string): string {
  let out = '';
  let last = 0;
  let from = 0;
  for (;;) {
    const at = text.indexOf('@', from);
    if (at === -1) break;
    from = at + 1;
    let s = at;
    while (s > last && isLocal(text.charCodeAt(s - 1))) s--;
    for (let k = at - 1; k >= s; k--) {
      if (text.charCodeAt(k) === 61) {
        s = k + 1;
        break;
      }
    }
    if (s === at) continue;
    let e = at + 1;
    while (e < text.length && isDomain(text.charCodeAt(e))) e++;
    while (e > at + 1 && text.charCodeAt(e - 1) === 46) e--;
    if (!DOMAIN_RE.test(text.slice(at + 1, e))) continue;
    out += text.slice(last, s) + '[email]';
    last = e;
    from = e;
  }
  return last === 0 ? text : out + text.slice(last);
}

/**
 * @internal exported for tests. Input should already be bounded
 * (MAX_WORK_CHARS); every step is linear in its length.
 */
export function redact(text: string): string {
  const t = text
    .replace(AUTH_RE, (_m, pre: string, val: string) => pre + maskValue(val))
    .replace(BEARER_RE, '$1 [redacted]')
    .replace(KEY_RE, (_m, key: string, sep: string, val: string) => key + sep + maskValue(val));
  return redactEmail(redactJwt(t));
}

/**
 * Drop a trailing run of token characters that was cut off mid-token. Plain
 * words are kept: only runs containing a digit or one of . @ % + / = - or an
 * `eyJ` prefix are treated as possible token fragments.
 */
function dropPartialTail(text: string): string {
  let i = text.length;
  while (i > 0 && isTailChar(text.charCodeAt(i - 1))) i--;
  const run = text.slice(i);
  if (run.length > 0 && (/[0-9.@%+/=-]/.test(run) || run.startsWith('eyJ'))) {
    return text.slice(0, i);
  }
  return text;
}

/** Thrown inside the serialiser once the output budget is spent. */
const BUDGET_SPENT = { budget: true };

/**
 * A JSON.stringify look-alike that stops walking as soon as MAX_WORK_CHARS of
 * output exist (so a huge array or object costs O(budget), not O(size)).
 * Arrays are capped at MAX_ARRAY_ITEMS. Cycles and BigInt throw like
 * JSON.stringify (the caller turns that into `[unserializable]`).
 */
function boundedJson(root: unknown): { text: string; clipped: boolean } | undefined {
  const out: string[] = [];
  let size = 0;
  const seen = new Set<object>();
  const emit = (s: string): void => {
    out.push(s);
    size += s.length;
    if (size > MAX_WORK_CHARS) throw BUDGET_SPENT;
  };
  // Returns false when the value has no JSON form (undefined, function, symbol).
  const walk = (value: unknown, key: string): boolean => {
    let v = value;
    if (typeof v === 'object' && v !== null) {
      const toJson = (v as { toJSON?: unknown }).toJSON;
      if (typeof toJson === 'function') v = (toJson as (k: string) => unknown).call(v, key);
    }
    switch (typeof v) {
      case 'string':
        emit(JSON.stringify(v.length > MAX_WORK_CHARS ? v.slice(0, MAX_WORK_CHARS) : v));
        return true;
      case 'number':
        emit(isFinite(v) ? String(v) : 'null');
        return true;
      case 'boolean':
        emit(String(v));
        return true;
      case 'bigint':
        throw new TypeError('bigint');
      case 'object': {
        if (v === null) {
          emit('null');
          return true;
        }
        if (seen.has(v)) throw new TypeError('circular');
        seen.add(v);
        if (Array.isArray(v)) {
          emit('[');
          const n = Math.min(v.length, MAX_ARRAY_ITEMS);
          for (let i = 0; i < n; i++) {
            if (i > 0) emit(',');
            if (!walk(v[i], String(i))) emit('null');
          }
          if (v.length > n) emit(`,"…[+${v.length - n}]"`);
          emit(']');
        } else {
          emit('{');
          let first = true;
          for (const k in v as Record<string, unknown>) {
            if (!Object.prototype.hasOwnProperty.call(v, k)) continue;
            const mark = out.length;
            const markSize = size;
            emit((first ? '' : ',') + JSON.stringify(k) + ':');
            if (walk((v as Record<string, unknown>)[k], k)) first = false;
            else {
              out.length = mark;
              size = markSize;
            }
          }
          emit('}');
        }
        seen.delete(v);
        return true;
      }
      default:
        return false;
    }
  };
  try {
    if (!walk(root, '')) return undefined;
    return { text: out.join(''), clipped: false };
  } catch (e) {
    if (e === BUDGET_SPENT) return { text: out.join('').slice(0, MAX_WORK_CHARS), clipped: true };
    throw e;
  }
}

interface Serialised {
  text: string;
  clipped: boolean;
}

function clip(text: string): Serialised {
  return text.length > MAX_WORK_CHARS
    ? { text: text.slice(0, MAX_WORK_CHARS), clipped: true }
    : { text, clipped: false };
}

function serialiseArg(a: unknown): Serialised {
  try {
    let r: Serialised;
    if (typeof a === 'string') r = clip(a);
    else if (a instanceof Error) r = clip(`${a.name}: ${a.message}`);
    else if (typeof a === 'bigint') r = { text: `${a.toString()}n`, clipped: false };
    else {
      const json = typeof a === 'object' && a !== null ? boundedJson(a) : undefined;
      r = json ?? clip(String(a));
    }
    // The clip marker is ours: never let user text carry it.
    return r.text.indexOf(CLIP) === -1 ? r : { ...r, text: r.text.split(CLIP).join('') };
  } catch {
    return { text: UNSERIALIZABLE, clipped: false };
  }
}

/** UTF-8 byte length without relying on TextEncoder (absent on some RN runtimes). */
function utf8Bytes(str: string): number {
  let bytes = 0;
  for (let i = 0; i < str.length; i++) {
    const c = str.charCodeAt(i);
    if (c < 0x80) bytes += 1;
    else if (c < 0x800) bytes += 2;
    else if (c >= 0xd800 && c <= 0xdbff) {
      const n = i + 1 < str.length ? str.charCodeAt(i + 1) : 0;
      if (n >= 0xdc00 && n <= 0xdfff) {
        bytes += 4; // valid surrogate pair
        i++;
      } else bytes += 3; // lone high surrogate
    } else bytes += 3;
  }
  return bytes;
}

function push(level: LogEntry['level'], args: unknown[]): void {
  try {
    const parts: string[] = [];
    let len = 0;
    let clipped = false;
    for (const a of args) {
      if (len > MAX_WORK_CHARS) {
        clipped = true;
        break;
      }
      const r = serialiseArg(a);
      parts.push(r.clipped ? r.text + CLIP : r.text);
      if (r.clipped) clipped = true;
      len += r.text.length + 1;
    }
    let joined = parts.join(' ');
    if (joined.length > MAX_WORK_CHARS) {
      joined = joined.slice(0, MAX_WORK_CHARS) + CLIP;
      clipped = true;
    }

    // Redact first (on a bounded string), then handle the clips.
    const segments = redact(joined).split(CLIP);
    let msg = '';
    for (let k = 0; k < segments.length; k++) {
      msg +=
        k < segments.length - 1 ? dropPartialTail(segments[k]) + TRUNCATION_MARKER : segments[k];
    }
    if (msg.length > MAX_LINE_CHARS) {
      const continues = isTailChar(msg.charCodeAt(MAX_LINE_CHARS));
      msg = msg.slice(0, MAX_LINE_CHARS);
      if (continues) msg = dropPartialTail(msg);
      msg += TRUNCATION_MARKER;
    } else if (clipped && msg.indexOf(TRUNCATION_MARKER) === -1) {
      msg += TRUNCATION_MARKER; // the clip point was swallowed by a redaction
    }

    if (buffer.length >= MAX_ENTRIES) {
      buffer.shift();
    }
    buffer.push({ level, ts: new Date().toISOString(), msg });
  } catch {
    // The logger must never break the app.
  }
}

export const SessionLogger = {
  /**
   * Patch console.warn and console.error to also write into the buffer.
   * Safe to call multiple times — subsequent calls are no-ops.
   */
  init(): void {
    if (initialised) return;
    initialised = true;

    originalWarn = console.warn.bind(console);
    originalError = console.error.bind(console);

    console.warn = (...args: unknown[]) => {
      push('warn', args); // never throws
      originalWarn?.(...args);
    };

    console.error = (...args: unknown[]) => {
      push('error', args); // never throws
      originalError?.(...args);
    };
  },

  /**
   * Return the buffer as a plain-text string suitable for attaching to a
   * GitHub issue. Most recent entries are at the bottom.
   */
  getLogs(): string {
    // Keep the newest lines whose total UTF-8 size (newlines included) fits
    // in MAX_LOG_BYTES; the oldest lines are dropped first.
    const lines: string[] = [];
    let total = 0;
    for (let i = buffer.length - 1; i >= 0; i--) {
      const e = buffer[i];
      const line = `[${e.ts}] ${e.level.toUpperCase()} ${e.msg}`;
      const cost = utf8Bytes(line) + (lines.length > 0 ? 1 : 0);
      if (total + cost > MAX_LOG_BYTES) break;
      total += cost;
      lines.push(line);
    }
    return lines.reverse().join('\n');
  },

  /** Expose entry count — used in tests. */
  get size(): number {
    return buffer.length;
  },

  /** Clear the buffer and restore console — used in tests. */
  _reset(): void {
    buffer.length = 0;
    if (initialised && originalWarn && originalError) {
      console.warn = originalWarn;
      console.error = originalError;
    }
    initialised = false;
    originalWarn = null;
    originalError = null;
  },
};

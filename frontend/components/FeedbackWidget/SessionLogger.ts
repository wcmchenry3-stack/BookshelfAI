/**
 * SessionLogger — lightweight circular buffer that captures recent console
 * warnings and errors so they can be attached to feedback submissions.
 *
 * Call `SessionLogger.init()` once at app startup (before any other imports
 * that might log). The buffer wraps at MAX_ENTRIES so memory use is bounded.
 * Pipeline per line: bounded serialise -> cut to MAX_WORK_CHARS -> redact ->
 * cut to MAX_LINE_CHARS (dropping any partial token left at the cut). getLogs()
 * is capped at MAX_LOG_BYTES.
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
const MAX_ARRAY_ITEMS = 100;

const isTok = (c: number): boolean =>
  (c >= 48 && c <= 57) || (c >= 65 && c <= 90) || (c >= 97 && c <= 122) || c === 45 || c === 95;
const isLocal = (c: number): boolean => isTok(c) || c === 46 || c === 37 || c === 43 || c === 39; // . % + '
const isDomain = (c: number): boolean => isTok(c) || c === 46;

// AUTH, BEARER and PARAM start on a fixed keyword followed by single character
// classes with no nested quantifiers, so they are linear. JWT and email are
// manual scans, because "class+ then literal" regexes are quadratic on inputs
// like 'a'.repeat(n).
const AUTH_RE =
  /(Authorization\s*:\s*)(?:(?:Basic|Bearer|Digest|Token|Negotiate|NTLM|ApiKey)\s+)?[^\s"',;]+/gi;
const BEARER_RE = /\b(Bearer)\s+[A-Za-z0-9\-._~+/]{8,}=*/gi;
const PARAM_RE =
  /\b(access_token|refresh_token|id_token|token|api[_-]?key|password|secret)=[^&\s"']+/gi;

/** Replace JWT-shaped tokens (eyJ.seg.seg). Linear: one pass, candidates must start at a token boundary. */
function redactJwt(text: string): string {
  let out = '';
  let last = 0;
  let from = 0;
  for (;;) {
    const i = text.indexOf('eyJ', from);
    if (i === -1) break;
    from = i + 3;
    if (i > 0 && isTok(text.charCodeAt(i - 1))) continue; // inside a longer run
    let p = i + 3;
    while (p < text.length && isTok(text.charCodeAt(p))) p++;
    if (text.charCodeAt(p) !== 46) continue;
    let q = p + 1;
    while (q < text.length && isTok(text.charCodeAt(q))) q++;
    if (q === p + 1 || text.charCodeAt(q) !== 46) continue;
    let r = q + 1;
    while (r < text.length && isTok(text.charCodeAt(r))) r++;
    out += text.slice(last, i) + '[jwt]';
    last = r;
    from = r;
  }
  return last === 0 ? text : out + text.slice(last);
}

const DOMAIN_RE = /^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$/; // classes are disjoint: linear

/** Replace email addresses by expanding left/right from each '@'. Linear overall. */
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
    .replace(AUTH_RE, '$1[redacted]')
    .replace(BEARER_RE, '$1 [redacted]')
    .replace(PARAM_RE, '$1=[redacted]');
  return redactEmail(redactJwt(t));
}

/** Characters that can be part of a token / email that a cut may have split. */
const isTailChar = (c: number): boolean => isLocal(c) || c === 64 || c === 47 || c === 61;

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

/** JSON.stringify with a size budget: arrays are capped and containers stop being expanded once the budget is spent. */
function boundedJson(a: unknown): string | undefined {
  let size = 0;
  const json = JSON.stringify(a, (key, value: unknown) => {
    if (size > MAX_WORK_CHARS) return typeof value === 'object' && value !== null ? '…' : undefined;
    size += key.length + (typeof value === 'string' ? value.length : 8);
    if (Array.isArray(value) && value.length > MAX_ARRAY_ITEMS) {
      return [...value.slice(0, MAX_ARRAY_ITEMS), `…[+${value.length - MAX_ARRAY_ITEMS}]`];
    }
    return value;
  });
  return json === undefined ? undefined : json.slice(0, MAX_WORK_CHARS);
}

function serialiseArg(a: unknown): string {
  try {
    if (typeof a === 'string') return a.slice(0, MAX_WORK_CHARS);
    if (a instanceof Error) return `${a.name}: ${a.message}`.slice(0, MAX_WORK_CHARS);
    if (typeof a === 'bigint') return `${a.toString()}n`;
    if (typeof a === 'object' && a !== null) {
      const json = boundedJson(a);
      if (json !== undefined) return json;
    }
    return String(a).slice(0, MAX_WORK_CHARS);
  } catch {
    return UNSERIALIZABLE;
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
    for (const a of args) {
      if (len > MAX_WORK_CHARS) break;
      const part = serialiseArg(a);
      parts.push(part);
      len += part.length + 1;
    }
    let msg = parts.join(' ');
    let truncated = msg.length > MAX_WORK_CHARS;
    if (truncated) msg = dropPartialTail(redact(msg.slice(0, MAX_WORK_CHARS)));
    else msg = redact(msg);
    if (msg.length > MAX_LINE_CHARS) {
      const continues = isTailChar(msg.charCodeAt(MAX_LINE_CHARS));
      msg = msg.slice(0, MAX_LINE_CHARS);
      if (continues) msg = dropPartialTail(msg);
      truncated = true;
    }
    if (truncated) msg += TRUNCATION_MARKER;

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

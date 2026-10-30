/**
 * SessionLogger — lightweight circular buffer that captures recent console
 * warnings and errors so they can be attached to feedback submissions.
 *
 * Call `SessionLogger.init()` once at app startup (before any other imports
 * that might log). The buffer wraps at MAX_ENTRIES so memory use is bounded.
 * Lines are cut at MAX_LINE_CHARS, secrets (Bearer tokens, JWTs, emails) are
 * redacted before storage, and getLogs() is capped at MAX_LOG_BYTES.
 */

const MAX_ENTRIES = 200;
/** Each stored line (message part) is cut at this many characters. */
const MAX_LINE_CHARS = 1000;
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

// All patterns are linear-time: single character classes with no nested or
// overlapping quantifiers. They only ever run on text already cut to
// MAX_LINE_CHARS, which bounds worst-case work.
const BEARER_RE = /Bearer\s+[A-Za-z0-9\-._~+/]+=*/gi;
const JWT_RE = /eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*/g;
const EMAIL_RE = /[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+/g;

function redact(text: string): string {
  return text
    .replace(BEARER_RE, 'Bearer [redacted]')
    .replace(JWT_RE, '[jwt]')
    .replace(EMAIL_RE, '[email]');
}

function serialiseArg(a: unknown): string {
  try {
    if (typeof a === 'string') return a;
    if (a instanceof Error) return `${a.name}: ${a.message}`;
    if (typeof a === 'bigint') return `${a.toString()}n`;
    const json = JSON.stringify(a);
    return json === undefined ? String(a) : json;
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
    else if (c >= 0xd800 && c <= 0xdbff && i + 1 < str.length) {
      bytes += 4; // surrogate pair
      i++;
    } else bytes += 3;
  }
  return bytes;
}

function push(level: LogEntry['level'], args: unknown[]): void {
  try {
    let msg = args.map(serialiseArg).join(' ');
    // Truncate first so the regexes never see a huge string.
    const truncated = msg.length > MAX_LINE_CHARS;
    if (truncated) msg = msg.slice(0, MAX_LINE_CHARS);
    msg = redact(msg);
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
      try {
        push('warn', args);
      } catch {
        // never let logging interfere with the original call
      }
      originalWarn!(...args);
    };

    console.error = (...args: unknown[]) => {
      try {
        push('error', args);
      } catch {
        // never let logging interfere with the original call
      }
      originalError!(...args);
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

/**
 * Narrow Sentry `beforeSend` filter for expected, user-environment network
 * failures (#701, follow-up to #660 / #663).
 *
 * Drops only two signatures seen as web `onunhandledrejection` events:
 *  (a) the web font loader's `"<n>ms timeout exceeded"` (expo-font /
 *      fontfaceobserver) when no stack frame is first-party code;
 *  (b) a bare `NetworkError` DOMException (code 19, "A network error
 *      occurred.") with no stack frames at all.
 *
 * Everything else is kept: our API client's axios errors (`AxiosError`,
 * "Network Error" / "timeout of 30000ms exceeded"), errors with a first-party
 * frame, other timeouts, other network errors, malformed events.
 * To widen it, see docs/claude/frontend.md ("Sentry noise filter").
 */

type Frame = { filename?: unknown };
type ExceptionValue = {
  type?: unknown;
  value?: unknown;
  stacktrace?: { frames?: unknown };
  mechanism?: { type?: unknown };
};
type EventLike = { exception?: { values?: unknown } };
type HintLike = { originalException?: unknown } | undefined;

const FONT_TIMEOUT_RE = /^\d+ms timeout exceeded$/;
const NETWORK_ERROR_TEXT = 'A network error occurred.';
const UNHANDLED_REJECTION = 'auto.browser.global_handlers.onunhandledrejection';
// The bundled web entry (Expo): .../_expo/static/js/web/entry-<hash>.js
const BUNDLED_ENTRY_RE = /\/_expo\/static\/js\/[^?#]*\.js(?:[?#].*)?$/;

function isBundledOrAnonymous(frame: Frame): boolean {
  const f = frame?.filename;
  if (f === undefined || f === null || f === '' || f === '<anonymous>') return true;
  return typeof f === 'string' && BUNDLED_ENTRY_RE.test(f);
}

function getFrames(ex: ExceptionValue): Frame[] | null {
  const frames = ex.stacktrace?.frames;
  if (frames === undefined || frames === null) return [];
  return Array.isArray(frames) ? (frames as Frame[]) : null;
}

function isUnhandledRejection(ex: ExceptionValue): boolean {
  const t = ex.mechanism?.type;
  return t === UNHANDLED_REJECTION || t === 'onunhandledrejection';
}

function isNetworkErrorDomException(ex: ExceptionValue, hint: HintLike): boolean {
  const orig = hint?.originalException as { name?: unknown; code?: unknown } | undefined;
  if (orig && typeof orig === 'object' && orig.name === 'NetworkError' && orig.code === 19) {
    return true;
  }
  const { type, value } = ex;
  if (typeof value !== 'string') return false;
  return (
    (type === 'NetworkError' && value === NETWORK_ERROR_TEXT) ||
    value === `NetworkError: ${NETWORK_ERROR_TEXT}`
  );
}

/** True only for the known-benign signatures. Never throws. */
export function isExpectedClientNetworkNoise(event: unknown, hint?: unknown): boolean {
  try {
    const values = (event as EventLike | null | undefined)?.exception?.values;
    if (!Array.isArray(values) || values.length !== 1) return false;
    const ex = values[0] as ExceptionValue;
    if (!ex || typeof ex !== 'object' || !isUnhandledRejection(ex)) return false;
    const frames = getFrames(ex);
    if (frames === null) return false;

    // (a) font loader timeout: every frame is bundled/anonymous, none first-party
    if (typeof ex.value === 'string' && FONT_TIMEOUT_RE.test(ex.value)) {
      return frames.every(isBundledOrAnonymous);
    }
    // (b) bare NetworkError DOMException: no stack frames at all
    if (isNetworkErrorDomException(ex, hint as HintLike)) {
      return frames.length === 0;
    }
    return false;
  } catch {
    return false;
  }
}

/**
 * Sentry `beforeSend`: returns null for expected noise, else the event
 * unchanged. `onDrop` lets the caller leave a breadcrumb instead of dropping
 * silently.
 */
export function filterExpectedNetworkNoise<T>(
  event: T,
  hint?: unknown,
  onDrop?: () => void
): T | null {
  if (isExpectedClientNetworkNoise(event, hint)) {
    try {
      onDrop?.();
    } catch {
      // never throw from beforeSend
    }
    return null;
  }
  return event;
}

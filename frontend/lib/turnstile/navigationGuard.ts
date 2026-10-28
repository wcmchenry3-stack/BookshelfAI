import { originOf, TURNSTILE_ORIGIN } from './config';

/**
 * The hidden Turnstile WebView may load only its own inline page (served
 * under `baseOrigin`), Turnstile's script and challenge frames, and blank
 * frames (#740). Everything else is blocked, in any frame.
 */
export function isAllowedTurnstileNavigation(url: string, baseOrigin: string): boolean {
  if (url === 'about:blank' || url === 'about:srcdoc') return true;
  const origin = originOf(url);
  return origin !== null && (origin === baseOrigin || origin === TURNSTILE_ORIGIN);
}

/** `originWhitelist` for the WebView: only these origins (plus about:blank). */
export function turnstileOriginWhitelist(baseOrigin: string): string[] {
  return [baseOrigin, TURNSTILE_ORIGIN, 'about:srcdoc'];
}

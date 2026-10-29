import { originOf, TURNSTILE_ORIGIN } from './config';

/**
 * The hidden Turnstile WebView may load only its own inline page (the exact
 * `baseOrigin` URL it is served under, not any other path on that origin),
 * Turnstile's script and challenge frames (https, any path), and blank
 * frames (#740). Everything else is blocked, in any frame.
 */
export function isAllowedTurnstileNavigation(url: string, baseOrigin: string): boolean {
  if (url === 'about:blank' || url === 'about:srcdoc') return true;
  // The initial page load (iOS reports the base URL, with or without "/").
  if (url === baseOrigin || url === `${baseOrigin}/`) return true;
  return originOf(url) === TURNSTILE_ORIGIN;
}

/**
 * `originWhitelist` for the WebView. Deliberately everything: react-native-
 * webview hands a URL that fails the whitelist to `Linking.openURL` (the
 * system browser) instead of just blocking it, so all filtering is done by
 * `isAllowedTurnstileNavigation` in `onShouldStartLoadWithRequest`, where a
 * refused load is simply dropped.
 */
export const TURNSTILE_ORIGIN_WHITELIST: readonly string[] = ['*'];

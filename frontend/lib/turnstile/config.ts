/**
 * Cloudflare Turnstile configuration for POST /scan (#740).
 *
 * Both values are public (they ship in the app bundle). Read at call time so
 * tests can change them; Expo still inlines `process.env.EXPO_PUBLIC_*`.
 *
 *   EXPO_PUBLIC_TURNSTILE_SITE_KEY  the widget's site key. Unset (or not a
 *                                   plausible key): no widget is loaded and
 *                                   /scan is sent without a token, as before.
 *   EXPO_PUBLIC_TURNSTILE_BASE_URL  native only: the https origin the hidden
 *                                   WebView's inline page claims. Turnstile
 *                                   checks this hostname against the widget's
 *                                   allowed hostnames. Default: the origin of
 *                                   EXPO_PUBLIC_API_URL.
 */

/** The only origin the Turnstile script and challenge frames come from. */
export const TURNSTILE_ORIGIN = 'https://challenges.cloudflare.com';
export const TURNSTILE_SCRIPT_URL = `${TURNSTILE_ORIGIN}/turnstile/v0/api.js?render=explicit`;

/** How long a /scan attempt waits for a token before it is sent without one. */
export const TURNSTILE_TOKEN_TIMEOUT_MS = 10_000;

/** The form field the server reads (backend/app/api/scan.py). */
export const TURNSTILE_FORM_FIELD = 'cf-turnstile-response';

// Site keys are short ASCII identifiers (e.g. 0x4AAAAAAA..., test keys 1x00...).
// Anything else is rejected rather than written into the widget page.
const SITE_KEY_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;

export function turnstileSiteKey(): string | null {
  const raw = process.env.EXPO_PUBLIC_TURNSTILE_SITE_KEY?.trim();
  return raw && SITE_KEY_PATTERN.test(raw) ? raw : null;
}

/**
 * The scheme://host[:port] of an absolute http(s) URL, lower-cased, or null.
 * (React Native's URL has no `origin`, so this is parsed by hand.) URLs with
 * credentials are rejected.
 */
export function originOf(url: string | null | undefined): string | null {
  if (!url) return null;
  const m = /^(https?):\/\/([^/?#\\]+)(?:[/?#\\]|$)/i.exec(url.trim());
  if (!m || m[2].includes('@')) return null;
  return `${m[1].toLowerCase()}://${m[2].toLowerCase()}`;
}

function isLocalOrigin(origin: string): boolean {
  return /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(origin);
}

/**
 * The origin the native widget page is loaded under: https, or http only for
 * localhost (dev with a Turnstile test key). Null when none is configured.
 */
export function turnstileBaseUrl(): string | null {
  const origin =
    originOf(process.env.EXPO_PUBLIC_TURNSTILE_BASE_URL) ??
    originOf(process.env.EXPO_PUBLIC_API_URL);
  if (!origin) return null;
  return origin.startsWith('https://') || isLocalOrigin(origin) ? origin : null;
}

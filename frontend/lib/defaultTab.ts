/**
 * The single place that defines where the app lands after sign-in and on
 * launch with a valid stored session (#904, #913).
 *
 * The user's "Open on" choice is stored on the server (`users.default_tab`,
 * returned by `GET /auth/me`, set with `PATCH /users/me/preferences`). Any
 * value the app does not know, `null` (never set), or no answer at all
 * (offline, an error, too slow) means Scan.
 */
import { api } from './api';

/** The screens a user can open on; the same values as the backend's `DEFAULT_TABS`. */
export const DEFAULT_TABS = ['scan', 'wishlist', 'my-books'] as const;
export type DefaultTab = (typeof DEFAULT_TABS)[number];

/** What the app opens on when the user has not chosen, or the choice is unknown. */
export const FALLBACK_DEFAULT_TAB: DefaultTab = 'scan';

const ROUTES = {
  scan: '/(tabs)/scan',
  wishlist: '/(tabs)/wishlist',
  'my-books': '/(tabs)/my-books',
} as const satisfies Record<DefaultTab, string>;

type DefaultTabRoute = (typeof ROUTES)[DefaultTab];

/**
 * How long sign-in waits for `GET /auth/me` to learn the user's choice before
 * landing on Scan. Launch never waits on it: it reads the value from the
 * session check it already makes.
 */
export const DEFAULT_TAB_FETCH_TIMEOUT_MS = 3000;

export function isDefaultTab(value: unknown): value is DefaultTab {
  return typeof value === 'string' && (DEFAULT_TABS as readonly string[]).includes(value);
}

/** The tab a server value means: itself when known, else Scan. */
export function parseDefaultTab(value: unknown): DefaultTab {
  return isDefaultTab(value) ? value : FALLBACK_DEFAULT_TAB;
}

/** The route for a stored value (anything unknown, null or missing is Scan). */
export function defaultTabRoute(value: unknown): DefaultTabRoute {
  return ROUTES[parseDefaultTab(value)];
}

/** `default_tab` from a `GET /auth/me` body, as a tab (Scan when absent or unknown). */
export function defaultTabFromMe(data: unknown): DefaultTab {
  const value =
    data && typeof data === 'object' ? (data as { default_tab?: unknown }).default_tab : null;
  return parseDefaultTab(value);
}

/**
 * The user's choice from `GET /auth/me`, or `null` if the call fails or takes
 * longer than `timeoutMs` (so a caller can tell "unknown" from Scan). Never
 * rejects.
 */
export async function loadDefaultTab(
  timeoutMs: number = DEFAULT_TAB_FETCH_TIMEOUT_MS
): Promise<DefaultTab | null> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<null>((resolve) => {
    timer = setTimeout(() => resolve(null), timeoutMs);
  });
  const request = (async (): Promise<DefaultTab | null> => {
    try {
      const { data } = await api.get('/auth/me', { timeout: timeoutMs });
      return defaultTabFromMe(data);
    } catch {
      return null;
    }
  })();
  try {
    return await Promise.race([request, timeout]);
  } finally {
    clearTimeout(timer);
  }
}

/** The user's choice from `GET /auth/me`, or Scan when it cannot be read in time. */
export async function fetchDefaultTab(
  timeoutMs: number = DEFAULT_TAB_FETCH_TIMEOUT_MS
): Promise<DefaultTab> {
  return (await loadDefaultTab(timeoutMs)) ?? FALLBACK_DEFAULT_TAB;
}

/** Saves the user's choice; resolves to what the server stored. Rejects on failure. */
export async function saveDefaultTab(tab: DefaultTab): Promise<DefaultTab> {
  const { data } = await api.patch('/users/me/preferences', { default_tab: tab });
  return defaultTabFromMe(data);
}

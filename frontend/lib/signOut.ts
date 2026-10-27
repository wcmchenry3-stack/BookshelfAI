import { REFRESH_TOKEN_KEY, api } from './api';
import * as storage from './storage';

/**
 * Best-effort server-side logout: revokes the refresh token so it can't be
 * replayed. Any failure (offline, 403, 5xx, missing token) is ignored; the
 * caller still clears the local session. (An in-flight token refresh could
 * rotate the token mid sign-out; accepted, the stale token just stays unrevoked.)
 */
export const SERVER_LOGOUT_TIMEOUT_MS = 5000;

export async function serverLogout(): Promise<void> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const refreshToken = await storage.getItem(REFRESH_TOKEN_KEY);
    if (!refreshToken) return;
    // The axios timeout bounds the request; the race also bounds a stalled
    // socket so it can never block the local sign-out.
    await Promise.race([
      api.post(
        '/auth/logout',
        { refresh_token: refreshToken },
        { timeout: SERVER_LOGOUT_TIMEOUT_MS }
      ),
      new Promise<void>((resolve) => {
        timer = setTimeout(resolve, SERVER_LOGOUT_TIMEOUT_MS);
      }),
    ]);
  } catch {
    // Intentionally ignored.
  } finally {
    if (timer) clearTimeout(timer);
  }
}

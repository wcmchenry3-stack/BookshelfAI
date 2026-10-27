import { REFRESH_TOKEN_KEY, api, waitForPendingRefresh } from './api';
import * as storage from './storage';

export const SERVER_LOGOUT_TIMEOUT_MS = 5000;

/**
 * Best-effort server-side logout: revokes the refresh token so it can't be
 * replayed. Any failure (offline, 403, 5xx, timeout, missing token) is
 * ignored; the caller still clears the local session. If the call fails or
 * times out, the token stays valid server-side until it expires.
 *
 * An in-flight token refresh is awaited first (inside the same time bound),
 * so the token revoked is the rotated one and the refresh cannot write a
 * fresh token pair after the caller has logged out.
 */
export async function serverLogout(): Promise<void> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    // The axios timeout bounds the request; the race also bounds a stalled
    // socket or refresh so it can never block the local sign-out.
    await Promise.race([
      (async () => {
        await waitForPendingRefresh();
        const refreshToken = await storage.getItem(REFRESH_TOKEN_KEY);
        if (!refreshToken) return;
        await api.post(
          '/auth/logout',
          { refresh_token: refreshToken },
          { timeout: SERVER_LOGOUT_TIMEOUT_MS }
        );
      })(),
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

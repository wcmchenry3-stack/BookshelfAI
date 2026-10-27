import { REFRESH_TOKEN_KEY, api } from './api';
import * as storage from './storage';

/**
 * Best-effort server-side logout: revokes the refresh token so it can't be
 * replayed. Any failure (offline, 403, 5xx, missing token) is ignored; the
 * caller still clears the local session.
 */
export async function serverLogout(): Promise<void> {
  try {
    const refreshToken = await storage.getItem(REFRESH_TOKEN_KEY);
    if (!refreshToken) return;
    await api.post('/auth/logout', { refresh_token: refreshToken });
  } catch {
    // Intentionally ignored.
  }
}
